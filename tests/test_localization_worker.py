"""Localization Worker module tests (#24, spec §2G/§2H, §16.8/§16.10).

Follows the same fixture pattern as ``tests/test_localization_batch.py``:
a real Winner is driven through the actual Experiments v2 flow, then
through the real ``app.localization.batch``/``claims``/``truth``/``copy``
command sequence up to (and including) ``confirm()``, against the shared
suite-wide database (``app.adapters.persistence.database.engine``,
migrated once via Alembic in ``setUpClass``). Worker handlers are then
exercised through their public entry points only.
"""
from __future__ import annotations

import itertools
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.adapters.persistence.database import engine, tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    ExperimentDecisionRecord,
    GenerationRunRecord,
    JobRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    LocalizationAttemptOutputRecord,
    LocalizationAttemptRecord,
    LocalizationBatchCommandRecord,
    LocalizationBatchRecord,
    LocalizationItemProviderCallRecord,
    LocalizationItemRecord,
    LocalizationOutputPublicationRecord,
    LocalizationQcRecordRecord,
    LocalizedAssetLineageRecord,
    LocalizedCopyVersionRecord,
    MarketAssetBindingRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    MetaAccountRecord,
    MetaSyncRunRecord,
    PlanRecord,
    ProductRecord,
    QualityReviewRecord,
    SourceClaimInventoryRecord,
    WorkspaceRecord,
    new_id,
)
from app.entitlements.admission import PaidOperationAdmission
from app.entitlements.cost_models import ProviderCallAttemptRecord, SpendGuardrailRecord
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
from app.main import app
from app.localization import worker
from app.localization.batch import ItemNotRetryableError, confirm, create_draft, resume_item, retry_item
from app.localization.batch import BatchMarketTargetInput, BatchSourceInput
from app.localization.batch import start_preparation, submit_claim_decisions
from app.localization.claims import ClaimDecisionInput, create_inventory_from_analysis
from app.localization.copy import TranslationRecipe, approve_copy_version
from app.localization.eligibility import ValidationReport
from app.localization.evidence_tier import EvidenceTier
from app.localization.fact_sources import ManualFactSet, ManualFactSourceAdapter
from app.localization.truth import (
    FactPolicy,
    MarketResolutionCommand,
    SourceStep,
    TruthResolutionCommand,
)
from app.localization.queries import get_item
from app.localization import batch as batch_mod
from app.localization import claims as claims_mod
from app.localization import copy as copy_mod
from app.platform.operations import OperationRecord
from app.platform.job_outcomes import (
    HandlerOutcomeSignal,
    JobFailed,
    JobObsolete,
    JobUncertain,
)
from app.platform.tenancy import TenantContext
from app.platform.http_identity import request_tenant_context
from app.providers.dispatch import CoordinatedCallResult, ExternalCallCoordinator
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)
from tests.support.varchar_widths import assert_varchar_widths

WS_ID = "ws_lw_worker"
CTX = TenantContext(workspace_id=WS_ID, role="owner", initiator_id="user_1", initiator_role="owner")

_MARKET_ID_COUNTER = itertools.count(1)

PRICE_REGISTRY = {
    "mock-vision": {"mock-vision-1": Decimal("0.01")},
    "mock-edit": {"mock-edit-1": Decimal("0.05")},
    "mock-translate": {"mock-translate-1": Decimal("0.02")},
}


# ---------------------------------------------------------------------------
# Fixture helpers (mirrors tests/test_localization_batch.py)
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
        session.add(WorkspaceRecord(id=ws_id, name=f"Worker test {ws_id}"))
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
        ctx, product_id=_product_id(ws_id), name=f"LW-{tag}", draft_context=_draft_context(pv_id, ov_id),
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


def _seed_review(ctx: TenantContext, asset_version_id: str, *, value: str = "approved", created_at=None) -> None:
    with tenant_session(ctx) as s:
        if value == "approved" and created_at is None:
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
        kwargs = dict(
            id=new_id("qr"), workspace_id=ctx.workspace_id, product_id=_product_id(ctx.workspace_id),
            asset_id=av.asset_id, asset_version_id=asset_version_id,
            review_value=value, source="human", actor_id="reviewer_1",
        )
        if created_at is not None:
            kwargs["created_at"] = created_at
        s.add(QualityReviewRecord(**kwargs))
        s.flush()


def _make_market_policy_version(
    session, market_id: str, *, restricted_claim_types=(), qc_rules_json=None,
    locale: str = "en-US",
) -> MarketPolicyVersionRecord:
    policy = MarketPolicyRecord(
        id=new_id("mp"), market_id=market_id, locale=locale,
        country=market_id[:2], currency="USD",
    )
    session.add(policy)
    session.flush()
    version = MarketPolicyVersionRecord(
        id=new_id("mpv"), market_policy_id=policy.id, version_number=1, version_state="effective",
        locale=locale, country=policy.country, currency="USD",
        pricing_context_json={}, digit_format_json={}, base_tone="neutral",
        reserved_words_json=[], restricted_claim_types_json=list(restricted_claim_types),
        requires_local_copy=False, shipping_address_granularity="country",
        max_fact_staleness_seconds=86400, qc_rules_json=qc_rules_json or {},
    )
    session.add(version)
    session.flush()
    return version


_SQLITE_CHECKS_WIDENED = False


def _widen_sqlite_check_constraint(conn, table_name: str, *, old_fragment: str, new_fragment: str) -> None:
    """Widen exactly one CHECK constraint's value list on one already-
    migrated table, in place, on SQLite only -- every other column,
    constraint and index (including ones this Alembic history never
    actually applied to SQLite) is preserved byte-for-byte.

    ``migrations/versions/u1a2b3c4d5e6_localization_truth_bounded_qc.py``
    widens two pre-existing tables' CHECK constraints (``provider_call
    _attempts.step_kind`` to admit the Localization step kinds;
    ``asset_versions.origin_kind`` to admit ``'localized'``) only under its
    own ``if _is_pg(conn):`` branches -- SQLite cannot ``ALTER TABLE ...
    DROP/ADD CONSTRAINT`` at all, and neither branch added a SQLite-
    compatible table-rebuild fallback (unlike every other CHECK constraint
    change in the same migration, which SQLAlchemy's ``batch_alter_table``
    already handles on both dialects). A SQLite-backed ``Base.metadata
    .create_all()`` database (e.g. ``tests/test_provider_dispatch.py``'s
    fixture) never hits this because it builds each table directly from
    the current, correct ORM CHECK constraint -- only the Alembic-migrated
    path used here (and by every other Localization test module) is
    affected, and only once a caller actually inserts a row the *old*
    CHECK would reject (a ``ProviderCallAttemptRecord`` with a
    Localization ``step_kind``, or an ``AssetVersionRecord`` with
    ``origin_kind='localized'``) -- nothing before this Worker module
    (Task 16) ever did.

    This module is the first real ``ExternalCallCoordinator`` caller and
    the first to publish a localized child AssetVersion (see
    ``app/localization/worker.py``'s module docstring), so it is the first
    to need the fix. Rather than editing the shared migration (out of this
    task's scope and higher-blast-radius), this rewrites the table's own
    ``sqlite_master`` DDL text -- substituting only the one CHECK clause --
    and rebuilds from that. Recreating from the *current ORM model*
    instead (tried first) pulled in several newer CHECK/FK constraints
    (``chargeability_consistency``, ``truth_required``, the truth_packages
    FK, ...) that were *also* never applied to SQLite by this same PG-only
    gap, and that plenty of already-committed rows from other test modules
    predate and violate -- retroactively enforcing those against a shared
    suite-wide database breaks unrelated tests. Substituting only the
    named fragment is deliberately the narrowest fix: every other
    constraint (or absence of one) stays exactly as it already was on
    SQLite. Idempotent (a second call finds ``old_fragment`` already gone
    and no-ops) and safe to run every test session against the shared
    suite-wide SQLite database.
    """
    row = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,),
    ).fetchone()
    if row is None or old_fragment not in row[0]:
        return
    old_sql = row[0]
    new_sql = old_sql.replace(old_fragment, new_fragment)

    cols = conn.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    col_names = ", ".join(c[1] for c in cols)
    legacy_name = f"{table_name}_pre_lw"
    # Indices keep their own name across `RENAME TABLE` in SQLite (only the
    # table they point at changes) -- drop them first so the recreated
    # table can claim the same names back.
    indices = conn.exec_driver_sql(f"PRAGMA index_list({table_name})").fetchall()
    conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    # Modern SQLite (legacy_alter_table=OFF, the default) rewrites every
    # *other* table's FK text to follow a renamed table -- exactly the
    # opposite of what a same-name rebuild needs: child tables must keep
    # referencing the literal original name, which the freshly (re)created
    # table then satisfies again. Legacy mode leaves their FK text alone
    # across the rename.
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
# TestCase
# ---------------------------------------------------------------------------


class WorkerTestBase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        _widen_sqlite_localization_check_constraints()

        now = datetime.now(timezone.utc)
        with tenant_session(CTX) as s:
            _ensure_workspace(s, WS_ID)
            # Every WorkerTestBase subclass runs its own setUpClass against
            # the same shared suite-wide database (WS_ID is shared across
            # all three TestCase classes in this module) -- guard these
            # workspace-scoped singletons so a second/third subclass's
            # setUpClass is a no-op rather than a duplicate-key failure.
            if s.get(EntitlementProjectionRecord, WS_ID) is None:
                s.add(EntitlementProjectionRecord(
                    workspace_id=WS_ID, entitlement_version=1, lease_sequence=1,
                    projection_version=1, schema_version=2, semantic_hash="h" * 8,
                    valid_until=now + timedelta(days=30), issued_at=now - timedelta(days=1),
                    period_start=now - timedelta(days=1), period_end=now + timedelta(days=29),
                    resolved_entitlements_json={},
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

    def _build_ready_batch(
        self, tag: str, *, external_qc_steps: tuple[str, ...] = (), qc_rules_json: dict | None = None,
    ) -> dict:
        winner = _build_confirmed_winner(WS_ID, CTX, tag)
        _seed_review(CTX, winner["asset_version_id"])

        with tenant_session(CTX) as s:
            # "LW" (Localization Worker) namespaces this file's market_id
            # values distinctly from every other Localization test module
            # sharing the same suite-wide database and UNIQUE(market_id) --
            # tests/test_localization_batch.py independently counts from 1
            # under its own "US" prefix, and the two would otherwise collide.
            mpv = _make_market_policy_version(
                s, f"LW{next(_MARKET_ID_COUNTER):04d}"[:10], qc_rules_json=qc_rules_json,
            )
            mpv_id = mpv.id

        with tenant_session(CTX) as s:
            row = s.get(MarketPolicyVersionRecord, mpv_id)
            mp = s.get(MarketPolicyRecord, row.market_policy_id)
            market = mp.market_id

        truth_cmd = self._truth_command_for(
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
            from app.localization.batch import start_preparation
            start_preparation(
                s, CTX, batch_id=batch_id, truth_command=truth_cmd, analysis_recipe_hash=recipe_hash,
                analysis_provider="mock-vision", analysis_model="mock-vision-1",
                analysis_adapter_version="mock-v1", actor_id="user_1", admission=self.admission,
            )

        with tenant_session(CTX) as s:
            from app.localization.batch import submit_claim_decisions
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
            from app.localization.batch import approve_copy
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
                edit_adapter_version="mock-edit-v1", external_qc_steps=external_qc_steps,
                actor_id="user_1", role="owner", admission=self.admission,
            )

        item_id = confirmed.item_ids[0]
        return {
            "batch_id": batch_id, "item_id": item_id,
            "asset_version_id": winner["asset_version_id"], "decision_id": winner["decision_id"],
            "market": market,
        }

    def _truth_command_for(
        self, tag: str, pv_id: str, ov_id: str, mpv_rows: list[tuple[str, str]],
    ) -> TruthResolutionCommand:
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
        return TruthResolutionCommand(
            product_version_id=pv_id, offer_version_id=ov_id,
            truth_resolution_policy_version_id=policy_version_id, truth_data_mode="real",
            max_staleness_seconds=86400 * 30, resolution_rule_version="truth-rule-v1",
            markets=tuple(market_cmds), now=now,
        )

    # -- shared handler-driving helpers -------------------------------

    async def _run_edit(
        self, batch_id: str, item_id: str, attempt_number: int = 1
    ) -> HandlerOutcomeSignal | None:
        """Run the handler the way the Worker fence does.

        The fence catches the handler's outcome signal and maps it onto the
        Job lifecycle; here it is returned so a test can assert on it
        instead of the signal escaping as an error.
        """
        try:
            await worker.handle_edit_attempt(
                CTX, batch_id=batch_id, item_id=item_id, attempt_number=attempt_number,
                session_factory=tenant_session, coordinator=self.coordinator,
            )
        except HandlerOutcomeSignal as signal:
            return signal
        return None

    def _item(self, item_id: str) -> LocalizationItemRecord:
        with tenant_session(CTX) as s:
            return s.get(LocalizationItemRecord, item_id)

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


class TestEditAttemptHandler(WorkerTestBase):
    async def test_accepts_only_authorized_queued_attempt(self) -> None:
        built = self._build_ready_batch("edit_guard01")
        item_id, batch_id = built["item_id"], built["batch_id"]

        # Not authorized/queued: item already succeeded (simulate via a
        # direct state write since there is no public "succeeded" path
        # this early) -- the handler must no-op, never raise.
        with tenant_session(CTX) as s:
            s.execute(
                update(LocalizationItemRecord)
                .where(LocalizationItemRecord.id == item_id)
                .values(state="succeeded", revision=LocalizationItemRecord.revision + 1)
            )

        await self._run_edit(batch_id, item_id, attempt_number=1)

        with tenant_session(CTX) as s:
            attempts = s.scalars(
                select(LocalizationAttemptRecord).where(LocalizationAttemptRecord.item_id == item_id)
            ).all()
        self.assertTrue(all(a.state == "authorized" for a in attempts))

    async def test_rechecks_eligibility_before_call(self) -> None:
        built = self._build_ready_batch("edit_elig01")
        item_id, batch_id = built["item_id"], built["batch_id"]

        # Revoke source eligibility (a later, more recent review rejects
        # the exact source AssetVersion) before the edit call ever runs.
        _seed_review(
            CTX, built["asset_version_id"], value="rejected",
            created_at=datetime.now(timezone.utc) + timedelta(seconds=5),
        )

        await self._run_edit(batch_id, item_id, attempt_number=1)

        item = self._item(item_id)
        self.assertEqual(item.state, "paused")
        self.assertEqual(item.reason_code, "ineligible_before_dispatch")

        with tenant_session(CTX) as s:
            calls = s.scalars(
                select(LocalizationItemProviderCallRecord).where(
                    LocalizationItemProviderCallRecord.item_id == item_id,
                )
            ).all()
            outputs = s.scalars(
                select(LocalizationAttemptOutputRecord).where(LocalizationAttemptOutputRecord.item_id == item_id)
            ).all()
        self.assertEqual(calls, [])
        self.assertEqual(outputs, [])

    async def test_persists_output_before_qc(self) -> None:
        built = self._build_ready_batch("edit_persist01")
        item_id, batch_id = built["item_id"], built["batch_id"]

        await self._run_edit(batch_id, item_id, attempt_number=1)

        item = self._item(item_id)
        self.assertEqual(item.state, "running")

        attempt = self._attempt(item_id, batch_id, 1)
        self.assertEqual(attempt.state, "completed")

        with tenant_session(CTX) as s:
            output = s.scalar(
                select(LocalizationAttemptOutputRecord).where(LocalizationAttemptOutputRecord.attempt_id == attempt.id)
            )
            provider_attempt = s.scalar(
                select(ProviderCallAttemptRecord).where(
                    ProviderCallAttemptRecord.workspace_id == WS_ID,
                    ProviderCallAttemptRecord.run_item_id.is_(None),
                    ProviderCallAttemptRecord.step_kind == "localization_edit",
                    ProviderCallAttemptRecord.truth_package_id.is_not(None),
                ).order_by(ProviderCallAttemptRecord.prepared_at.desc())
            )
            qc_job = s.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == WS_ID, JobRecord.type == "localization.run_deterministic_qc",
                    JobRecord.resource_id == item_id,
                )
            )
        self.assertIsNotNone(output)
        self.assertIsNotNone(provider_attempt)
        self.assertEqual(len(provider_attempt.call_semantic_key_hash), 64)
        self.assertIsNotNone(qc_job)

    async def test_rechecks_eligibility_after_response(self) -> None:
        built = self._build_ready_batch("edit_elig_after01")
        item_id, batch_id = built["item_id"], built["batch_id"]

        valid_report = ValidationReport(valid=True, findings=())
        invalid_report = ValidationReport(valid=False, findings=("revoked_in_flight",))
        with patch(
            "app.localization.worker.validate_source_for_batch",
            side_effect=[valid_report, invalid_report],
        ):
            await self._run_edit(batch_id, item_id, attempt_number=1)

        item = self._item(item_id)
        self.assertEqual(item.state, "paused")
        self.assertEqual(item.reason_code, "ineligible_after_dispatch")

        attempt = self._attempt(item_id, batch_id, 1)
        with tenant_session(CTX) as s:
            output = s.scalar(
                select(LocalizationAttemptOutputRecord).where(LocalizationAttemptOutputRecord.attempt_id == attempt.id)
            )
            qc_job = s.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == WS_ID, JobRecord.type == "localization.run_deterministic_qc",
                    JobRecord.resource_id == item_id,
                )
            )
        # Output stays isolated -- persisted, never discarded.
        self.assertIsNotNone(output)
        # But QC was never enqueued for it.
        self.assertIsNone(qc_job)

    async def test_resume_after_dispatch_pause_never_redispatches_edit(self) -> None:
        """spec §2H/§16.10: an Item paused *after* its edit call already
        succeeded must never have that edit redispatched on resume -- the
        isolated output is re-used, and `resume_item()`'s "queued" requeue
        is re-routed straight back into the (idempotent, cost-free)
        deterministic-QC fold instead of a second `ExternalCallCoordinator
        .execute()`."""
        built = self._build_ready_batch("edit_resume01")
        item_id, batch_id = built["item_id"], built["batch_id"]

        valid_report = ValidationReport(valid=True, findings=())
        invalid_report = ValidationReport(valid=False, findings=("revoked_in_flight",))
        with patch(
            "app.localization.worker.validate_source_for_batch",
            side_effect=[valid_report, invalid_report],
        ):
            await self._run_edit(batch_id, item_id, attempt_number=1)

        item = self._item(item_id)
        self.assertEqual(item.state, "paused")
        attempt = self._attempt(item_id, batch_id, 1)
        self.assertEqual(attempt.state, "completed")

        with tenant_session(CTX) as s:
            resume_item(
                s, CTX, batch_id=batch_id, item_id=item_id,
                expected_item_revision=item.revision, actor_id="user_1",
            )
        item = self._item(item_id)
        self.assertEqual(item.state, "queued", "eligibility is fine again by resume time")

        await self._run_edit(batch_id, item_id, attempt_number=1)

        # No second edit dispatch: still exactly one output row for this
        # Attempt, and the Item advanced past `running` via the QC fold
        # rather than being redispatched back to `paused`/`running` again.
        with tenant_session(CTX) as s:
            outputs = s.scalars(
                select(LocalizationAttemptOutputRecord).where(LocalizationAttemptOutputRecord.attempt_id == attempt.id)
            ).all()
            qc_job = s.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == WS_ID, JobRecord.type == "localization.run_deterministic_qc",
                    JobRecord.resource_id == item_id,
                )
            )
        self.assertEqual(len(outputs), 1)
        self.assertIsNotNone(qc_job)

        self._run_deterministic_qc(batch_id, item_id, attempt.id)
        item = self._item(item_id)
        self.assertEqual(item.state, "running")
        self._finalize(batch_id, item_id, attempt.id)
        item = self._item(item_id)
        self.assertEqual(item.state, "succeeded")

    async def test_deterministic_qc_failure_cancels_external_qc(self) -> None:
        built = self._build_ready_batch(
            "edit_qcfail01", external_qc_steps=("ocr_check",),
            qc_rules_json={"forbidden_terms": ["hello"]},
        )
        item_id, batch_id = built["item_id"], built["batch_id"]

        await self._run_edit(batch_id, item_id, attempt_number=1)
        attempt = self._attempt(item_id, batch_id, 1)

        with tenant_session(CTX) as s:
            ocr_job_before = s.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == WS_ID, JobRecord.type == "localization.ocr_check",
                    JobRecord.resource_id == item_id,
                )
            )
        self.assertIsNotNone(ocr_job_before, "confirm() should have pre-admitted the ocr_check Job")

        self._run_deterministic_qc(batch_id, item_id, attempt.id)

        item = self._item(item_id)
        self.assertEqual(item.state, "retry_wait")
        self.assertEqual(item.reason_code, "deterministic_qc_failed")

        with tenant_session(CTX) as s:
            ocr_job_after = s.get(JobRecord, ocr_job_before.id)
            qc_records = s.scalars(
                select(LocalizationQcRecordRecord).where(LocalizationQcRecordRecord.attempt_id == attempt.id)
            ).all()
        self.assertEqual(ocr_job_after.state, "cancelled")
        self.assertTrue(any(r.result == "fail" for r in qc_records))

    async def test_all_blocking_pass_enqueues_finalize(self) -> None:
        built = self._build_ready_batch("edit_finalize_enq01")
        item_id, batch_id = built["item_id"], built["batch_id"]

        await self._run_edit(batch_id, item_id, attempt_number=1)
        attempt = self._attempt(item_id, batch_id, 1)
        self._run_deterministic_qc(batch_id, item_id, attempt.id)

        with tenant_session(CTX) as s:
            finalize_job = s.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == WS_ID, JobRecord.type == "localization.finalize_item",
                    JobRecord.resource_id == item_id,
                )
            )
        self.assertIsNotNone(finalize_job)

    async def test_attempt3_failure_is_terminal(self) -> None:
        built = self._build_ready_batch(
            "edit_exhaust01", qc_rules_json={"forbidden_terms": ["hello"]},
        )
        item_id, batch_id = built["item_id"], built["batch_id"]

        for attempt_number in (1, 2, 3):
            await self._run_edit(batch_id, item_id, attempt_number=attempt_number)
            attempt = self._attempt(item_id, batch_id, attempt_number)
            self._run_deterministic_qc(batch_id, item_id, attempt.id)

            item = self._item(item_id)
            if attempt_number < 3:
                self.assertEqual(item.state, "retry_wait", f"attempt {attempt_number}")
                with tenant_session(CTX) as s:
                    retry_item(
                        s, CTX, batch_id=batch_id, item_id=item_id,
                        expected_item_revision=item.revision, actor_id="user_1", role="owner",
                        admission=self.admission,
                    )

        item = self._item(item_id)
        self.assertEqual(item.state, "failed")
        self.assertEqual(item.reason_code, "qc_exhausted")
        self.assertFalse(item.recoverable)

        with tenant_session(CTX) as s:
            fold_job = s.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == WS_ID,
                    JobRecord.type == "localization.fold_batch",
                    JobRecord.resource_id == batch_id,
                )
            )
        self.assertIsNotNone(fold_job)

        # A fourth attempt is not valid for this Item.
        with tenant_session(CTX) as s:
            with self.assertRaises((ItemNotRetryableError, batch_mod.AttemptExhaustedError)):
                retry_item(
                    s, CTX, batch_id=batch_id, item_id=item_id,
                    expected_item_revision=item.revision, actor_id="user_1", role="owner",
                    admission=self.admission,
                )

        with tenant_session(CTX) as s:
            worker.handle_fold_batch(s, CTX, batch_id=batch_id)
            batch = s.get(LocalizationBatchRecord, batch_id)
        self.assertEqual(batch.phase, "closed")
        self.assertEqual(batch.outcome, "failed")

    async def test_attempt3_provider_failure_enqueues_fold(self) -> None:
        built = self._build_ready_batch("edit_provider_exhaust01")
        item_id, batch_id = built["item_id"], built["batch_id"]
        self.coordinator.execute = AsyncMock(
            return_value=CoordinatedCallResult(
                attempt_id="pca_fake_failed",
                dispatch_certainty="not_dispatched",
                success=False,
                replayed=False,
                provider_request_id=None,
                evidence_reference=None,
                error_code="provider_rejected",
                error_detail="controlled failure",
                result=None,
            )
        )

        for attempt_number in (1, 2, 3):
            signal = await self._run_edit(
                batch_id,
                item_id,
                attempt_number=attempt_number,
            )
            # Each exhausted attempt is a determined Job failure, not a
            # success: the Operation must not aggregate to `succeeded`
            # for a provider call that produced nothing.
            self.assertIsInstance(signal, JobFailed)
            self.assertEqual(signal.code, "provider_rejected")
            item = self._item(item_id)
            if attempt_number < 3:
                self.assertEqual(item.state, "retry_wait")
                with tenant_session(CTX) as s:
                    retry_item(
                        s,
                        CTX,
                        batch_id=batch_id,
                        item_id=item_id,
                        expected_item_revision=item.revision,
                        actor_id="user_1",
                        role="owner",
                        admission=self.admission,
                    )

        item = self._item(item_id)
        self.assertEqual(item.state, "failed")
        self.assertEqual(item.reason_code, "qc_exhausted")
        with tenant_session(CTX) as s:
            fold_jobs = s.scalars(
                select(JobRecord).where(
                    JobRecord.workspace_id == WS_ID,
                    JobRecord.type == "localization.fold_batch",
                    JobRecord.resource_id == batch_id,
                )
            ).all()
        self.assertEqual(len(fold_jobs), 1)

    async def test_uncertain_dispatch_does_not_consume_attempt_slot(self) -> None:
        """Fix round 1, Finding C1 (spec §16.8: "Worker crash and uncertain
        must not repeat external calls"). A `dispatch_certainty=='uncertain'`
        outcome must move the Item to `uncertain` -- never `retry_wait`
        (which would burn a bounded Attempt slot) or `failed`."""
        built = self._build_ready_batch("edit_uncertain01")
        item_id, batch_id = built["item_id"], built["batch_id"]

        uncertain_result = CoordinatedCallResult(
            attempt_id="pca_fake_uncertain", dispatch_certainty="uncertain", success=False,
            replayed=False, provider_request_id=None, evidence_reference=None,
            error_code="timeout", error_detail="no response before deadline", result=None,
        )
        self.coordinator.execute = AsyncMock(return_value=uncertain_result)

        signal = await self._run_edit(batch_id, item_id, attempt_number=1)

        # The Job must land `uncertain` too -- a `succeeded` Job here would
        # let the Operation aggregate to `succeeded` for a call whose
        # outcome nobody knows (spec §16.8).
        self.assertIsInstance(signal, JobUncertain)
        self.assertEqual(signal.code, "timeout")

        item = self._item(item_id)
        self.assertEqual(item.state, "uncertain")
        self.assertEqual(item.reason_code, "dispatch_uncertain")
        self.assertNotIn(item.state, ("retry_wait", "failed"))

        attempt = self._attempt(item_id, batch_id, 1)
        self.assertEqual(attempt.state, "uncertain")

        # No output was actually persisted (the coordinator call was
        # faked, not a real completion) -- nothing publishable exists yet.
        with tenant_session(CTX) as s:
            output = s.scalar(
                select(LocalizationAttemptOutputRecord).where(LocalizationAttemptOutputRecord.attempt_id == attempt.id)
            )
        self.assertIsNone(output)

    async def test_running_state_reentry_dispatches_after_crash(self) -> None:
        """Fix round 1, Finding C2. A crash between the first session's
        `queued -> running` commit and the second session's
        `coordinator.execute()` call must not permanently strand the Item
        -- re-entering `handle_edit_attempt` while `running` (Attempt still
        `authorized`, no output yet) must actually dispatch, not no-op."""
        built = self._build_ready_batch("edit_crash01")
        item_id, batch_id = built["item_id"], built["batch_id"]

        # Simulate exactly that crash window: commit `running` directly,
        # without ever calling the handler (which would have gone on to
        # call the coordinator in the same invocation).
        with tenant_session(CTX) as s:
            s.execute(
                update(LocalizationItemRecord)
                .where(LocalizationItemRecord.id == item_id)
                .values(state="running", revision=LocalizationItemRecord.revision + 1)
            )
        item_before = self._item(item_id)
        self.assertEqual(item_before.state, "running")
        attempt_before = self._attempt(item_id, batch_id, 1)
        self.assertEqual(attempt_before.state, "authorized")

        await self._run_edit(batch_id, item_id, attempt_number=1)

        item = self._item(item_id)
        self.assertEqual(item.state, "running", "still running -- now past dispatch, awaiting QC")
        attempt = self._attempt(item_id, batch_id, 1)
        self.assertEqual(attempt.state, "completed")
        with tenant_session(CTX) as s:
            output = s.scalar(
                select(LocalizationAttemptOutputRecord).where(LocalizationAttemptOutputRecord.attempt_id == attempt.id)
            )
            qc_job = s.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == WS_ID, JobRecord.type == "localization.run_deterministic_qc",
                    JobRecord.resource_id == item_id,
                )
            )
        self.assertIsNotNone(output)
        self.assertIsNotNone(qc_job)


class TestPreparationHandlers(WorkerTestBase):
    """Fix round 1, Finding I6: basic smoke coverage for the two
    preparation-phase handlers, which follow the same two-session +
    ExternalCallCoordinator pattern as `handle_edit_attempt`."""

    def _draft_and_market(self, tag: str, *, locale: str = "en-US") -> dict:
        winner = _build_confirmed_winner(WS_ID, CTX, tag)
        _seed_review(CTX, winner["asset_version_id"])

        with tenant_session(CTX) as s:
            mpv = _make_market_policy_version(
                s, f"LW{next(_MARKET_ID_COUNTER):04d}"[:10], locale=locale
            )
            mpv_id = mpv.id
        with tenant_session(CTX) as s:
            row = s.get(MarketPolicyVersionRecord, mpv_id)
            mp = s.get(MarketPolicyRecord, row.market_policy_id)
            market = mp.market_id

        truth_cmd = self._truth_command_for(
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
        return {
            "batch_id": draft.batch_id, "asset_version_id": winner["asset_version_id"],
            "market": market, "truth_cmd": truth_cmd,
        }

    async def test_analyze_source_creates_inventory(self) -> None:
        tag = "prep_analyze01"
        built = self._draft_and_market(tag)
        recipe_hash = f"recipe-{tag}"

        with tenant_session(CTX) as s:
            sp = start_preparation(
                s, CTX, batch_id=built["batch_id"], truth_command=built["truth_cmd"],
                analysis_recipe_hash=recipe_hash, analysis_provider="mock-vision",
                analysis_model="mock-vision-1", analysis_adapter_version="mock-v1",
                actor_id="user_1", admission=self.admission,
            )
        self.assertEqual(sp.missing_analysis, (built["asset_version_id"],))
        with tenant_session(CTX) as s:
            no_inventory = s.scalar(
                select(SourceClaimInventoryRecord).where(
                    SourceClaimInventoryRecord.workspace_id == WS_ID,
                    SourceClaimInventoryRecord.asset_version_id == built["asset_version_id"],
                    SourceClaimInventoryRecord.analysis_recipe_hash == recipe_hash,
                )
            )
        self.assertIsNone(no_inventory, "nothing should exist before the handler runs")

        await worker.handle_analyze_source(
            CTX, batch_id=built["batch_id"], asset_version_id=built["asset_version_id"],
            analysis_recipe_hash=recipe_hash, session_factory=tenant_session, coordinator=self.coordinator,
        )

        with tenant_session(CTX) as s:
            inventory = s.scalar(
                select(SourceClaimInventoryRecord).where(
                    SourceClaimInventoryRecord.workspace_id == WS_ID,
                    SourceClaimInventoryRecord.asset_version_id == built["asset_version_id"],
                    SourceClaimInventoryRecord.analysis_recipe_hash == recipe_hash,
                )
            )
        self.assertIsNotNone(inventory)

    async def test_translate_copy_creates_translated_copy_version(self) -> None:
        tag = "prep_translate01"
        built = self._draft_and_market(tag, locale="fr-FR")
        recipe_hash = f"recipe-{tag}"

        with tenant_session(CTX) as s:
            create_inventory_from_analysis(
                s, CTX, asset_version_id=built["asset_version_id"], analysis_recipe_hash=recipe_hash,
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
                s, CTX, batch_id=built["batch_id"], truth_command=built["truth_cmd"],
                analysis_recipe_hash=recipe_hash, analysis_provider="mock-vision",
                analysis_model="mock-vision-1", analysis_adapter_version="mock-v1",
                actor_id="user_1", admission=self.admission,
            )

        with tenant_session(CTX) as s:
            scd = submit_claim_decisions(
                s, CTX, batch_id=built["batch_id"],
                expected_revision=s.get(
                    LocalizationBatchRecord, built["batch_id"]
                ).revision,
                asset_version_id=built["asset_version_id"],
                target_market=built["market"], analysis_recipe_hash=recipe_hash,
                decisions=[ClaimDecisionInput(claim_index=0, action="preserve")],
                copy_policy_snapshot={}, source_locale="en-US",
                target_locale="fr-FR",
                translation_recipe=TranslationRecipe(
                    execution_mode="mock", provider="mock-translate", model="mock-translate-1",
                    adapter_version="mock-translate-v1", mock_scenario="default", mock_version="v1",
                ),
                requested_evidence_tier=EvidenceTier.MOCK, actor_id="user_1", role="owner",
                admission=self.admission,
            )
        self.assertEqual(scd.copy_origin, "translation_pending")

        with tenant_session(CTX) as s:
            revision_before_translation = s.get(
                LocalizationBatchRecord, built["batch_id"]
            ).revision

        with tenant_session(CTX) as s:
            inventory = s.scalar(
                select(SourceClaimInventoryRecord).where(
                    SourceClaimInventoryRecord.workspace_id == WS_ID,
                    SourceClaimInventoryRecord.asset_version_id == built["asset_version_id"],
                    SourceClaimInventoryRecord.analysis_recipe_hash == recipe_hash,
                )
            )
            inventory_id = inventory.id

        await worker.handle_translate_copy(
            CTX, batch_id=built["batch_id"], asset_version_id=built["asset_version_id"],
            target_market=built["market"], inventory_id=inventory_id,
            claim_decision_set_id=scd.decision_set_id,
            session_factory=tenant_session, coordinator=self.coordinator,
        )

        with tenant_session(CTX) as s:
            copy_version = s.scalar(
                select(LocalizedCopyVersionRecord).where(
                    LocalizedCopyVersionRecord.workspace_id == WS_ID,
                    LocalizedCopyVersionRecord.claim_decision_set_id == scd.decision_set_id,
                    LocalizedCopyVersionRecord.copy_origin == "translated",
                )
            )
            batch = s.get(LocalizationBatchRecord, built["batch_id"])
        self.assertIsNotNone(copy_version)
        self.assertEqual(batch.phase, "awaiting_copy_approval")
        self.assertEqual(batch.preparation_stage, "copy")
        self.assertEqual(batch.revision, revision_before_translation + 1)
        text_segment = next(
            seg for seg in copy_version.content_json["segments"] if seg.get("kind") == "text"
        )
        self.assertIn("fr-FR", text_segment["text"])

        # The copy series is named by four ids -- far past jobs.resource_id's
        # varchar(100) on PostgreSQL. Admission stores it as a digest and the
        # handler above found its Job by that same digest.
        with tenant_session(CTX) as s:
            translate_job = s.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == WS_ID,
                    JobRecord.type == "localization.translate_copy",
                    JobRecord.resource_id == batch_mod.copy_series_resource_id(
                        built["asset_version_id"], built["market"], inventory_id, scd.decision_set_id,
                    ),
                )
            )
            self.assertIsNotNone(translate_job)
            self.assertLessEqual(len(translate_job.resource_id), JobRecord.__table__.c.resource_id.type.length)
            assert_varchar_widths(s, WS_ID)

        # The background phase transition invalidates the ETag observed
        # before translation completed.
        with self.assertRaises(batch_mod.StaleRevisionError):
            with tenant_session(CTX) as s:
                batch_mod.approve_copy(
                    s,
                    CTX,
                    batch_id=built["batch_id"],
                    expected_revision=revision_before_translation,
                    copy_version_id=copy_version.id,
                    actor_id="stale-client",
                    role="member",
                )

        # Re-running the projection after all persisted inputs are unchanged
        # must not churn ETags.
        with tenant_session(CTX) as s:
            locked = worker._lock_preparation_batch(
                s, CTX, built["batch_id"]
            )
            current_revision = locked.revision
            changed = batch_mod._refresh_preparation_projection(
                s,
                CTX,
                locked,
                bump_revision_on_change=True,
            )
            self.assertFalse(changed)
            self.assertEqual(locked.revision, current_revision)


class TestFinalizeItemHandler(WorkerTestBase):
    async def _run_to_qc_pending(self, tag: str) -> dict:
        built = self._build_ready_batch(tag)
        await self._run_edit(built["batch_id"], built["item_id"], attempt_number=1)
        attempt = self._attempt(built["item_id"], built["batch_id"], 1)
        built["attempt_id"] = attempt.id
        return built

    async def test_rechecks_all_before_publish(self) -> None:
        built = await self._run_to_qc_pending("finalize_recheck01")
        self._run_deterministic_qc(built["batch_id"], built["item_id"], built["attempt_id"])

        # Revoke copy approval directly (bypassing the public API, which
        # has no "un-approve" command) to simulate a runtime invalidation
        # discovered only at the finalize recheck.
        with tenant_session(CTX) as s:
            item = s.get(LocalizationItemRecord, built["item_id"])
            s.execute(
                update(LocalizedCopyVersionRecord)
                .where(LocalizedCopyVersionRecord.id == item.copy_version_id)
                .values(state="needs_review")
            )

        self._finalize(built["batch_id"], built["item_id"], built["attempt_id"])

        item = self._item(built["item_id"])
        self.assertEqual(item.state, "paused")
        self.assertIn("copy_not_approved", item.reason_detail or "")

        with tenant_session(CTX) as s:
            children = s.scalars(
                select(AssetVersionRecord).where(AssetVersionRecord.origin_kind == "localized")
            ).all()
        self.assertFalse(
            any(c.parent_version_id == built["asset_version_id"] for c in children),
            "no child AssetVersion should be created when a recheck fails",
        )

    async def test_creates_child_asset_version_lineage_binding(self) -> None:
        built = await self._run_to_qc_pending("finalize_child01")
        self._run_deterministic_qc(built["batch_id"], built["item_id"], built["attempt_id"])
        self._finalize(built["batch_id"], built["item_id"], built["attempt_id"])

        item = self._item(built["item_id"])
        self.assertEqual(item.state, "succeeded")

        with tenant_session(CTX) as s:
            child = s.scalar(
                select(AssetVersionRecord).where(
                    AssetVersionRecord.origin_kind == "localized",
                    AssetVersionRecord.parent_version_id == built["asset_version_id"],
                )
            )
            self.assertIsNotNone(child)
            self.assertEqual(child.truth_package_id, item.truth_package_id)

            lineage = s.scalar(
                select(LocalizedAssetLineageRecord).where(
                    LocalizedAssetLineageRecord.asset_version_id == child.id,
                )
            )
            self.assertIsNotNone(lineage)
            self.assertEqual(lineage.item_id, built["item_id"])
            self.assertEqual(lineage.source_asset_version_id, built["asset_version_id"])

            binding = s.scalar(
                select(MarketAssetBindingRecord).where(
                    MarketAssetBindingRecord.workspace_id == WS_ID,
                    MarketAssetBindingRecord.item_id == built["item_id"],
                    MarketAssetBindingRecord.active.is_(True),
                )
            )
            self.assertIsNotNone(binding)
            self.assertEqual(binding.binding_kind, "localized")
            self.assertEqual(binding.bound_asset_version_id, child.id)
            self.assertEqual(binding.publication_id, lineage.publication_id)
            self.assertEqual(binding.review_state, "pending_review")
            self.assertEqual(binding.truth_freshness, "current")
            self.assertFalse(binding.ready_for_experiment)

            projected = get_item(s, CTX, built["batch_id"], built["item_id"])
            self.assertEqual(
                projected["lineage"],
                {
                    "source_asset_version_id": built["asset_version_id"],
                    "parent_asset_version_id": built["asset_version_id"],
                    "decision_id": built["decision_id"],
                    "offer_version_id": lineage.offer_version_id,
                    "truth_package_id": item.truth_package_id,
                    "copy_version_id": item.copy_version_id,
                    "localized_asset_version_id": child.id,
                    "attempt_id": built["attempt_id"],
                    "output_id": lineage.output_id,
                    "publication_id": lineage.publication_id,
                },
            )
            self.assertEqual(
                projected["binding"],
                {
                    "id": binding.id,
                    "kind": "localized",
                    "bound_asset_version_id": child.id,
                    "review_state": "pending_review",
                    "truth_freshness": "current",
                    "ready_for_experiment": False,
                },
            )

            fold_job = s.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == WS_ID, JobRecord.type == "localization.fold_batch",
                    JobRecord.resource_id == built["batch_id"],
                )
            )
            self.assertIsNotNone(fold_job)

        app.dependency_overrides[request_tenant_context] = lambda: CTX
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    f"/api/v1/localization/batches/{built['batch_id']}"
                    f"/items/{built['item_id']}"
                )
        finally:
            app.dependency_overrides.pop(request_tenant_context, None)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["lineage"], projected["lineage"])

    async def test_binding_failure_rolls_back_entire_publication(self) -> None:
        built = await self._run_to_qc_pending("finalize_binding_rollback01")
        self._run_deterministic_qc(
            built["batch_id"], built["item_id"], built["attempt_id"]
        )

        with patch.object(
            worker.binding_mod,
            "publish_binding",
            side_effect=RuntimeError("controlled binding failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "controlled binding failure"):
                self._finalize(
                    built["batch_id"], built["item_id"], built["attempt_id"]
                )

        item = self._item(built["item_id"])
        self.assertEqual(item.state, "running")
        with tenant_session(CTX) as s:
            children = s.scalars(
                select(AssetVersionRecord).where(
                    AssetVersionRecord.parent_version_id == built["asset_version_id"],
                    AssetVersionRecord.origin_kind == "localized",
                )
            ).all()
            publications = s.scalars(
                select(LocalizationOutputPublicationRecord).where(
                    LocalizationOutputPublicationRecord.workspace_id == WS_ID,
                    LocalizationOutputPublicationRecord.item_id == built["item_id"],
                )
            ).all()
            lineages = s.scalars(
                select(LocalizedAssetLineageRecord).where(
                    LocalizedAssetLineageRecord.workspace_id == WS_ID,
                    LocalizedAssetLineageRecord.item_id == built["item_id"],
                )
            ).all()
            bindings = s.scalars(
                select(MarketAssetBindingRecord).where(
                    MarketAssetBindingRecord.workspace_id == WS_ID,
                    MarketAssetBindingRecord.item_id == built["item_id"],
                )
            ).all()
        self.assertEqual(children, [])
        self.assertEqual(publications, [])
        self.assertEqual(lineages, [])
        self.assertEqual(bindings, [])

    async def test_child_has_no_quality_review(self) -> None:
        built = await self._run_to_qc_pending("finalize_noqr01")
        self._run_deterministic_qc(built["batch_id"], built["item_id"], built["attempt_id"])
        self._finalize(built["batch_id"], built["item_id"], built["attempt_id"])

        with tenant_session(CTX) as s:
            child = s.scalar(
                select(AssetVersionRecord).where(
                    AssetVersionRecord.origin_kind == "localized",
                    AssetVersionRecord.parent_version_id == built["asset_version_id"],
                )
            )
            reviews = s.scalars(
                select(QualityReviewRecord).where(QualityReviewRecord.asset_version_id == child.id)
            ).all()
        self.assertEqual(reviews, [])


class TestIsolatedOutputRecovery(WorkerTestBase):
    """`authorize_isolated_qc` + `adopt_isolated_output` (spec §2E/§2H).

    The recovery pair had no coverage at all, which is how both shipped
    broken: authorization re-admitted the whole frozen QC plan (re-running
    and, for a chargeable step, re-billing checks that had already passed)
    and adoption published nothing -- it flipped the Item to `succeeded`
    while no AssetVersion, lineage or binding existed anywhere.
    """

    async def _isolated_attempt(
        self, tag: str, *, external_qc_steps: tuple[str, ...] = ()
    ) -> dict:
        """One Attempt whose edit call completed and whose output is
        preserved but unpublished -- the exact state recovery acts on."""
        built = self._build_ready_batch(tag, external_qc_steps=external_qc_steps)
        await self._run_edit(built["batch_id"], built["item_id"], attempt_number=1)
        attempt = self._attempt(built["item_id"], built["batch_id"], 1)
        built["attempt_id"] = attempt.id
        return built

    def _authorize(self, built: dict):
        with tenant_session(CTX) as s:
            return batch_mod.authorize_isolated_qc(
                s, CTX, batch_id=built["batch_id"], item_id=built["item_id"],
                attempt_id=built["attempt_id"], actor_id="user_1", role="owner",
                admission=self.admission,
            )

    def _adopt(self, built: dict):
        with tenant_session(CTX) as s:
            return batch_mod.adopt_isolated_output(
                s, CTX, batch_id=built["batch_id"], item_id=built["item_id"],
                attempt_id=built["attempt_id"], actor_id="user_1", role="owner",
            )

    def _attempt_jobs(self, attempt_id: str) -> list[JobRecord]:
        """Every Job `authorize_isolated_qc` scoped to this Attempt."""
        with tenant_session(CTX) as s:
            return list(
                s.scalars(
                    select(JobRecord).where(
                        JobRecord.workspace_id == WS_ID,
                        JobRecord.resource_type == "localization_attempt",
                        JobRecord.resource_id == attempt_id,
                    )
                )
            )

    def _qc_jobs_for_attempt(self, attempt_id: str) -> list[JobRecord]:
        """Only the *external* QC Jobs -- the chargeable ones an Operation
        is admitted for. The free deterministic re-run is asserted
        separately so a change to one cannot silently hide the other."""
        return [
            job
            for job in self._attempt_jobs(attempt_id)
            if job.type != "localization.run_deterministic_qc"
        ]

    def _deterministic_jobs_for_attempt(self, attempt_id: str) -> list[JobRecord]:
        return [
            job
            for job in self._attempt_jobs(attempt_id)
            if job.type == "localization.run_deterministic_qc"
        ]

    # -- authorize -------------------------------------------------------

    async def test_authorizes_only_the_steps_that_have_not_passed(self) -> None:
        built = await self._isolated_attempt(
            "iso_partial01", external_qc_steps=("ocr_check", "visual_qc")
        )
        # One of the two frozen steps already recorded a pass.
        with tenant_session(CTX) as s:
            worker.handle_ocr_check(
                s, CTX, batch_id=built["batch_id"], item_id=built["item_id"],
                attempt_id=built["attempt_id"],
            )

        result = self._authorize(built)

        self.assertEqual(result.authorized_steps, ("visual_qc",))
        job_types = sorted(job.type for job in self._qc_jobs_for_attempt(built["attempt_id"]))
        self.assertEqual(job_types, ["localization.visual_qc"])

    async def test_authorized_job_payload_carries_every_identifier(self) -> None:
        """`resource_id` on these Jobs is the *Attempt*, so the Worker
        cannot derive the Item from it the way it can for the Item-scoped
        QC Jobs `confirm()` admits."""
        built = await self._isolated_attempt(
            "iso_payload01", external_qc_steps=("ocr_check",)
        )
        self._authorize(built)

        jobs_for_attempt = self._qc_jobs_for_attempt(built["attempt_id"])
        self.assertEqual(len(jobs_for_attempt), 1)
        job = jobs_for_attempt[0]
        self.assertEqual(job.resource_type, "localization_attempt")
        self.assertEqual(job.resource_id, built["attempt_id"])
        self.assertEqual(job.payload_json["batch_id"], built["batch_id"])
        self.assertEqual(job.payload_json["item_id"], built["item_id"])
        self.assertEqual(job.payload_json["attempt_id"], built["attempt_id"])
        self.assertEqual(job.payload_json["step"], "ocr_check")

    async def test_authorizing_a_complete_plan_admits_no_operation(self) -> None:
        built = await self._isolated_attempt("iso_complete01")
        # The pipeline already ran this Attempt's deterministic plan, so
        # there is genuinely nothing left to authorize.
        self._run_deterministic_qc(
            built["batch_id"], built["item_id"], built["attempt_id"]
        )
        result = self._authorize(built)
        self.assertEqual(result.authorized_steps, ())
        self.assertFalse(result.deterministic_qc_enqueued)
        self.assertIsNone(result.operation_id)
        self.assertEqual(self._attempt_jobs(built["attempt_id"]), [])

    async def test_reauthorizing_the_same_gap_is_a_replay(self) -> None:
        built = await self._isolated_attempt(
            "iso_replay01", external_qc_steps=("ocr_check",)
        )
        first = self._authorize(built)
        second = self._authorize(built)

        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(second.operation_id, first.operation_id)
        self.assertEqual(len(self._qc_jobs_for_attempt(built["attempt_id"])), 1)
        self.assertEqual(
            len(self._deterministic_jobs_for_attempt(built["attempt_id"])), 1
        )

    # -- adopt -----------------------------------------------------------

    async def test_adoption_is_refused_while_external_qc_is_missing(self) -> None:
        built = await self._isolated_attempt(
            "iso_refuse01", external_qc_steps=("ocr_check",)
        )
        self._run_deterministic_qc(
            built["batch_id"], built["item_id"], built["attempt_id"]
        )

        with self.assertRaises(batch_mod.BatchValidationError) as caught:
            self._adopt(built)
        self.assertIn("qc_plan_incomplete", caught.exception.findings)
        self.assertIn("external_qc_missing:ocr_check", caught.exception.findings)

        item = self._item(built["item_id"])
        self.assertNotEqual(item.state, "succeeded")

    async def test_adoption_is_refused_while_deterministic_qc_never_ran(self) -> None:
        built = await self._isolated_attempt("iso_refuse_det01")

        with self.assertRaises(batch_mod.BatchValidationError) as caught:
            self._adopt(built)
        self.assertIn("qc_plan_incomplete", caught.exception.findings)

        with tenant_session(CTX) as s:
            publications = s.scalars(
                select(LocalizationOutputPublicationRecord).where(
                    LocalizationOutputPublicationRecord.workspace_id == WS_ID,
                    LocalizationOutputPublicationRecord.item_id == built["item_id"],
                )
            ).all()
        self.assertEqual(publications, [])

    async def test_adoption_publishes_the_same_evidence_as_finalize(self) -> None:
        built = await self._isolated_attempt(
            "iso_adopt01", external_qc_steps=("ocr_check",)
        )
        self._run_deterministic_qc(
            built["batch_id"], built["item_id"], built["attempt_id"]
        )
        self._authorize(built)
        with tenant_session(CTX) as s:
            worker.handle_ocr_check(
                s, CTX, batch_id=built["batch_id"], item_id=built["item_id"],
                attempt_id=built["attempt_id"],
            )

        result = self._adopt(built)

        self.assertFalse(result.replayed)
        item = self._item(built["item_id"])
        self.assertEqual(item.state, "succeeded")
        self.assertEqual(item.reason_code, "adopted_isolated_output")

        with tenant_session(CTX) as s:
            child = s.scalar(
                select(AssetVersionRecord).where(
                    AssetVersionRecord.origin_kind == "localized",
                    AssetVersionRecord.parent_version_id == built["asset_version_id"],
                )
            )
            self.assertIsNotNone(child)
            self.assertEqual(child.id, result.asset_version_id)

            publication = s.get(LocalizationOutputPublicationRecord, result.publication_id)
            self.assertEqual(publication.publication_kind, "adopted")
            self.assertIsNotNone(publication.recheck_hash)
            self.assertEqual(publication.actor_id, "user_1")

            lineage = s.scalar(
                select(LocalizedAssetLineageRecord).where(
                    LocalizedAssetLineageRecord.asset_version_id == child.id,
                )
            )
            self.assertIsNotNone(lineage)
            self.assertEqual(lineage.item_id, built["item_id"])
            self.assertEqual(lineage.publication_id, publication.id)

            binding = s.scalar(
                select(MarketAssetBindingRecord).where(
                    MarketAssetBindingRecord.workspace_id == WS_ID,
                    MarketAssetBindingRecord.item_id == built["item_id"],
                    MarketAssetBindingRecord.active.is_(True),
                )
            )
            self.assertIsNotNone(binding)
            self.assertEqual(binding.id, result.binding_id)
            self.assertEqual(binding.binding_kind, "localized")
            # spec §16.11: the localized child starts unreviewed, is not a
            # Winner, and is not experiment-ready.
            self.assertEqual(binding.review_state, "pending_review")
            self.assertFalse(binding.ready_for_experiment)

            reviews = s.scalars(
                select(QualityReviewRecord).where(
                    QualityReviewRecord.asset_version_id == child.id
                )
            ).all()
        self.assertEqual(reviews, [])

    async def test_adoption_through_the_route_publishes_in_the_api_transaction(self) -> None:
        """The route publishes the child AssetVersion in the API's own
        transaction, which on PostgreSQL runs as the API role (#46). Served
        over HTTP so the suite's grant guard checks those writes as the API
        role (#59); the evidence itself is asserted by the test above."""
        built = await self._isolated_attempt(
            "iso_adopt_route01", external_qc_steps=("ocr_check",)
        )
        self._run_deterministic_qc(
            built["batch_id"], built["item_id"], built["attempt_id"]
        )
        self._authorize(built)
        with tenant_session(CTX) as s:
            worker.handle_ocr_check(
                s, CTX, batch_id=built["batch_id"], item_id=built["item_id"],
                attempt_id=built["attempt_id"],
            )

        app.dependency_overrides[request_tenant_context] = lambda: CTX
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    f"/api/v1/localization/batches/{built['batch_id']}"
                    f"/items/{built['item_id']}/attempts/{built['attempt_id']}/adopt",
                    headers={"Idempotency-Key": "ik-iso-adopt-route01"},
                )
        finally:
            app.dependency_overrides.pop(request_tenant_context, None)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["replayed"])
        self.assertEqual(self._item(built["item_id"]).state, "succeeded")
        with tenant_session(CTX) as s:
            child = s.get(AssetVersionRecord, body["asset_version_id"])
            self.assertIsNotNone(child)
            self.assertEqual(child.origin_kind, "localized")
            self.assertEqual(child.parent_version_id, built["asset_version_id"])

    async def test_recovers_a_cancelled_item_from_refusal_to_published(self) -> None:
        """The whole recovery path on the state it actually happens in.

        Cancelling a Batch mid-dispatch is the canonical way an isolated
        output appears: the indivisible edit call completes anyway and its
        output is preserved, while the Item goes `cancelled` and every
        pipeline handler stops looking at it. Adoption must still be
        reachable -- authorize, let the recovery Jobs run, then publish.
        """
        built = await self._isolated_attempt(
            "iso_cancelled01", external_qc_steps=("ocr_check",)
        )
        with tenant_session(CTX) as s:
            batch = s.get(LocalizationBatchRecord, built["batch_id"])
            batch_mod.cancel(
                s, CTX, batch_id=built["batch_id"],
                expected_revision=batch.revision, actor_id="user_1",
            )
        item = self._item(built["item_id"])
        self.assertEqual(item.state, "cancelled")

        # The output survived the cancellation, isolated and unpublished.
        with tenant_session(CTX) as s:
            output = s.scalar(
                select(LocalizationAttemptOutputRecord).where(
                    LocalizationAttemptOutputRecord.workspace_id == WS_ID,
                    LocalizationAttemptOutputRecord.attempt_id == built["attempt_id"],
                )
            )
        self.assertIsNotNone(output)

        with self.assertRaises(batch_mod.BatchValidationError) as caught:
            self._adopt(built)
        self.assertIn("qc_plan_incomplete", caught.exception.findings)

        authorization = self._authorize(built)
        self.assertEqual(authorization.authorized_steps, ("ocr_check",))
        self.assertTrue(authorization.deterministic_qc_enqueued)

        # The recovery Jobs run against a Item that is *not* running. Under
        # the pipeline's own guard both of these would be silent no-ops and
        # adoption could never become reachable.
        with tenant_session(CTX) as s:
            worker.handle_run_deterministic_qc(
                s, CTX, batch_id=built["batch_id"], item_id=built["item_id"],
                attempt_id=built["attempt_id"], isolated=True,
            )
        with tenant_session(CTX) as s:
            worker.handle_ocr_check(
                s, CTX, batch_id=built["batch_id"], item_id=built["item_id"],
                attempt_id=built["attempt_id"], isolated=True,
            )

        # Recovery QC never advances the Item or publishes behind the user.
        self.assertEqual(self._item(built["item_id"]).state, "cancelled")
        with tenant_session(CTX) as s:
            finalize_jobs = s.scalars(
                select(JobRecord).where(
                    JobRecord.workspace_id == WS_ID,
                    JobRecord.type == "localization.finalize_item",
                    JobRecord.resource_id == built["item_id"],
                )
            ).all()
        self.assertEqual(finalize_jobs, [])

        result = self._adopt(built)
        self.assertFalse(result.replayed)
        self.assertTrue(result.asset_version_id)
        self.assertEqual(self._item(built["item_id"]).state, "succeeded")

    async def test_isolated_qc_ignores_an_attempt_with_no_output(self) -> None:
        """The recovery guard is "a completed Attempt holding an
        unpublished output", not "any Attempt" -- an authorized Job that
        finds neither must write nothing."""
        built = self._build_ready_batch("iso_noout01")
        attempt = self._attempt(built["item_id"], built["batch_id"], 1)
        self.assertIsNone(
            self._get_output(attempt.id), "fixture must start with no output"
        )

        with tenant_session(CTX) as s:
            worker.handle_ocr_check(
                s, CTX, batch_id=built["batch_id"], item_id=built["item_id"],
                attempt_id=attempt.id, isolated=True,
            )

        with tenant_session(CTX) as s:
            records = s.scalars(
                select(LocalizationQcRecordRecord).where(
                    LocalizationQcRecordRecord.workspace_id == WS_ID,
                    LocalizationQcRecordRecord.attempt_id == attempt.id,
                )
            ).all()
        self.assertEqual(records, [])

    def _get_output(self, attempt_id: str):
        with tenant_session(CTX) as s:
            return s.scalar(
                select(LocalizationAttemptOutputRecord).where(
                    LocalizationAttemptOutputRecord.workspace_id == WS_ID,
                    LocalizationAttemptOutputRecord.attempt_id == attempt_id,
                )
            )

    async def test_adoption_is_idempotent(self) -> None:
        built = await self._isolated_attempt("iso_idem01")
        self._run_deterministic_qc(
            built["batch_id"], built["item_id"], built["attempt_id"]
        )
        first = self._adopt(built)
        second = self._adopt(built)

        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(second.asset_version_id, first.asset_version_id)
        self.assertEqual(second.publication_id, first.publication_id)

        with tenant_session(CTX) as s:
            publications = s.scalars(
                select(LocalizationOutputPublicationRecord).where(
                    LocalizationOutputPublicationRecord.workspace_id == WS_ID,
                    LocalizationOutputPublicationRecord.item_id == built["item_id"],
                )
            ).all()
            children = s.scalars(
                select(AssetVersionRecord).where(
                    AssetVersionRecord.origin_kind == "localized",
                    AssetVersionRecord.parent_version_id == built["asset_version_id"],
                )
            ).all()
        self.assertEqual(len(publications), 1)
        self.assertEqual(len(children), 1)

    async def test_a_failed_adoption_rolls_the_whole_publication_back(self) -> None:
        built = await self._isolated_attempt("iso_rollback01")
        self._run_deterministic_qc(
            built["batch_id"], built["item_id"], built["attempt_id"]
        )

        from app.localization import publication as publication_mod

        with patch.object(
            publication_mod.binding_mod,
            "publish_binding",
            side_effect=RuntimeError("controlled binding failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "controlled binding failure"):
                self._adopt(built)

        item = self._item(built["item_id"])
        self.assertNotEqual(item.state, "succeeded")
        with tenant_session(CTX) as s:
            children = s.scalars(
                select(AssetVersionRecord).where(
                    AssetVersionRecord.origin_kind == "localized",
                    AssetVersionRecord.parent_version_id == built["asset_version_id"],
                )
            ).all()
            publications = s.scalars(
                select(LocalizationOutputPublicationRecord).where(
                    LocalizationOutputPublicationRecord.workspace_id == WS_ID,
                    LocalizationOutputPublicationRecord.item_id == built["item_id"],
                )
            ).all()
            lineages = s.scalars(
                select(LocalizedAssetLineageRecord).where(
                    LocalizedAssetLineageRecord.workspace_id == WS_ID,
                    LocalizedAssetLineageRecord.item_id == built["item_id"],
                )
            ).all()
            commands = s.scalars(
                select(LocalizationBatchCommandRecord).where(
                    LocalizationBatchCommandRecord.workspace_id == WS_ID,
                    LocalizationBatchCommandRecord.item_id == built["item_id"],
                    LocalizationBatchCommandRecord.command_kind
                    == "adopt_isolated_output",
                )
            ).all()
        self.assertEqual(children, [])
        self.assertEqual(publications, [])
        self.assertEqual(lineages, [])
        self.assertEqual(commands, [])


class TestCancelledSiblingsFoldTheOperation(WorkerTestBase):
    """`_cancel_unstarted_jobs` is this module's in-transaction twin of
    `jobs.cancel_resource`, so it owes the same Operation aggregation.

    A deterministic-QC failure cancels the confirm()-admitted external QC
    siblings. If that path did not fold, the paid Operation behind the
    command would sit `running` with every sibling Job terminal, waiting on
    a reconciliation sweep to notice.
    """

    async def test_cancelling_the_last_siblings_folds_the_operation(self) -> None:
        built = self._build_ready_batch(
            "cancel_fold01", external_qc_steps=("ocr_check", "visual_qc")
        )
        await self._run_edit(built["batch_id"], built["item_id"], attempt_number=1)

        with tenant_session(CTX) as s:
            job = s.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == WS_ID,
                    JobRecord.resource_id == built["item_id"],
                    JobRecord.type == "localization.edit_attempt",
                )
            )
            operation_id = job.operation_id
            # The edit Job settles the way the Worker fence settles it.
            job.state = "succeeded"
            job.finished_at = datetime.now(timezone.utc)
        self.assertIsNotNone(operation_id)

        with tenant_session(CTX) as s:
            operation = s.get(OperationRecord, operation_id)
            self.assertNotIn(operation.state, ("succeeded", "partially_succeeded"))

        with tenant_session(CTX) as s:
            cancelled = worker._cancel_unstarted_jobs(
                s, CTX, resource_type="localization_item",
                resource_id=built["item_id"],
            )
        self.assertGreater(cancelled, 0)

        with tenant_session(CTX) as s:
            operation = s.get(OperationRecord, operation_id)
        # One success, the rest cancelled before dispatch.
        self.assertEqual(operation.state, "partially_succeeded")


class TestPlaceholderQCHandlers(WorkerTestBase):
    """Fix round 1, Finding I6: smoke coverage for the local placeholder
    OCR/visual-QC handlers."""

    async def test_ocr_check_creates_qc_record_and_enqueues_finalize(self) -> None:
        built = self._build_ready_batch("ocr_check01", external_qc_steps=("ocr_check",))
        item_id, batch_id = built["item_id"], built["batch_id"]

        await self._run_edit(batch_id, item_id, attempt_number=1)
        attempt = self._attempt(item_id, batch_id, 1)
        self._run_deterministic_qc(batch_id, item_id, attempt.id)

        with tenant_session(CTX) as s:
            finalize_job_before = s.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == WS_ID, JobRecord.type == "localization.finalize_item",
                    JobRecord.resource_id == item_id,
                )
            )
        self.assertIsNone(finalize_job_before, "finalize must wait for the configured external QC step")

        with tenant_session(CTX) as s:
            worker.handle_ocr_check(s, CTX, batch_id=batch_id, item_id=item_id, attempt_id=attempt.id)

        with tenant_session(CTX) as s:
            qc_record = s.scalar(
                select(LocalizationQcRecordRecord).where(
                    LocalizationQcRecordRecord.attempt_id == attempt.id,
                    LocalizationQcRecordRecord.check_kind == "ocr_check",
                )
            )
            finalize_job = s.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == WS_ID, JobRecord.type == "localization.finalize_item",
                    JobRecord.resource_id == item_id,
                )
            )
        self.assertIsNotNone(qc_record)
        self.assertEqual(qc_record.result, "pass")
        self.assertIsNotNone(finalize_job)


class TestFoldBatchHandler(WorkerTestBase):
    async def test_fold_derives_succeeded_outcome(self) -> None:
        built = self._build_ready_batch("fold01")
        await self._run_edit(built["batch_id"], built["item_id"], attempt_number=1)
        attempt = self._attempt(built["item_id"], built["batch_id"], 1)
        self._run_deterministic_qc(built["batch_id"], built["item_id"], attempt.id)
        self._finalize(built["batch_id"], built["item_id"], attempt.id)

        with tenant_session(CTX) as s:
            worker.handle_fold_batch(s, CTX, batch_id=built["batch_id"])
            batch = s.get(LocalizationBatchRecord, built["batch_id"])
        self.assertEqual(batch.outcome, "succeeded")
        self.assertEqual(batch.phase, "closed")


def test_copy_series_resource_id_keeps_short_keys_and_digests_long_ones():
    width = JobRecord.__table__.c.resource_id.type.length
    assert batch_mod.copy_series_resource_id("av_1", "US", "sci_1", "lcds_1") == "av_1:US:sci_1:lcds_1"
    real = ("av_" + "a" * 32, "DE", "sci_" + "b" * 32, "lcds_" + "c" * 32)
    assert len(":".join(real)) > width
    stored = batch_mod.copy_series_resource_id(*real)
    assert len(stored) <= width
    assert stored == batch_mod.copy_series_resource_id(*real)
    assert stored != batch_mod.copy_series_resource_id(*real[:1], "FR", *real[2:])


if __name__ == "__main__":
    unittest.main()
