"""PostgreSQL evidence: CAS conflicts and composite FK enforcement (#24,
task 19, spec §3I).

Unlike ``tests/test_localization_rls.py`` and
``tests/test_localization_triggers.py`` -- which need a real PostgreSQL
cluster because RLS and BEFORE UPDATE/DELETE triggers only exist on that
dialect -- everything in this file runs on the shared suite-wide database
the same way the rest of the Localization test modules do
(``app.adapters.persistence.database.engine``, migrated once in
``setUpClass``):

* CAS (``UPDATE ... WHERE revision = expected_revision``) is application
  code, not a database feature, so it behaves identically on SQLite and
  PostgreSQL.
* Composite foreign keys are declared with plain SQLAlchemy
  ``ForeignKeyConstraint`` objects and enforced by SQLite too once
  ``PRAGMA foreign_keys=ON`` is set (``app.adapters.persistence.database``
  sets it on every SQLite connection).

Fixture builders are duplicated from ``tests/test_localization_batch.py``
and ``tests/test_localization_binding.py`` rather than imported, matching
this repo's existing convention of keeping every Localization test module
self-contained (see those files' own docstrings).

``StaleRevisionError`` tests
-----------------------------
``app.localization.batch.derive_batch_outcome`` takes a caller-supplied
``expected_revision`` and has no precondition that changes between calls
(every Item's terminal state is unaffected by the Batch's own outcome/
phase write), so two real, sequential calls that both start from the same
observed revision reproduce the race directly: the first is indistinguishable
from a winning concurrent writer, the second from a losing one.

``retry_item`` and ``binding.apply_dependency_change`` are not so simple:
``retry_item`` re-reads ``LocalizationItemRecord.state`` fresh on every call
and refuses anything but ``"retry_wait"``, so a second sequential call would
fail on that business precondition (state already moved to ``"queued"``)
before ever reaching the CAS ``UPDATE`` -- masking the very race the test
means to prove. ``apply_dependency_change`` manages its own ``expected``
revision internally (it has no caller-supplied parameter at all), so no
sequence of calls from one thread can ever race it. Both tests instead use
a deterministic ``before_cursor_execute`` hook that eagerly issues its own
revision bump, on the *same* connection, at the exact moment the function
under test is about to issue its own CAS ``UPDATE`` -- reproducing
"another writer moved the revision between this call's read and its
write" without real threads or timing-based sleeps (and without the
SQLite single-writer deadlock a genuinely separate connection would hit
against the still-open outer transaction; see
`_inject_concurrent_revision_bump`'s own docstring below).
"""
from __future__ import annotations

import itertools
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError

from app.adapters.persistence.database import engine, tenant_session
from app.adapters.persistence.models import (
    AdDailyMetricRecord,
    AssetRecord,
    AssetVersionRecord,
    ExperimentDecisionRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    LocalizationAttemptRecord,
    LocalizationBatchRecord,
    LocalizationItemRecord,
    LocalizationOutputPublicationRecord,
    MarketAssetBindingRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    MetaAccountRecord,
    MetaSyncRunRecord,
    PlanRecord,
    ProductRecord,
    QualityReviewRecord,
    TruthDerivedFactInputRecord,
    TruthDerivedFactRecord,
    TruthFactRecord,
    TruthPackageMarketRecord,
    TruthPackageRecord,
    TruthSourceAttemptRecord,
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
from app.localization.batch import (
    BatchMarketTargetInput,
    BatchSourceInput,
    StaleRevisionError,
    approve_copy,
    confirm,
    create_draft,
    derive_batch_outcome,
    retry_item,
    start_preparation,
    submit_claim_decisions,
)
from app.localization.binding import apply_dependency_change, publish_binding
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
from app.providers.dispatch import ExternalCallCoordinator
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)

WS_ID = "ws_lcas"
CTX = TenantContext(workspace_id=WS_ID, role="owner", initiator_id="user_1", initiator_role="owner")

_MARKET_ID_COUNTER = itertools.count(1)

PRICE_REGISTRY = {
    "mock-vision": {"mock-vision-1": Decimal("0.01")},
    "mock-edit": {"mock-edit-1": Decimal("0.05")},
    "mock-translate": {"mock-translate-1": Decimal("0.02")},
}


# ---------------------------------------------------------------------------
# Fixture helpers (mirrors tests/test_localization_binding.py)
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
        session.add(WorkspaceRecord(id=ws_id, name=f"CAS test {ws_id}"))
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
        ctx, product_id=_product_id(ws_id), name=f"LCAS-{tag}", draft_context=_draft_context(pv_id, ov_id),
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
    legacy_name = f"{table_name}_pre_lcas"
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


def _inject_concurrent_revision_bump(table: str, row_id: str):
    """A `before_cursor_execute` hook that, the first time it sees a
    statement whose SQL text is an ``UPDATE`` against `table`, eagerly
    issues its own ``revision += 1`` against `row_id` on the *same*
    connection, immediately before letting the intercepted statement run.

    SQLite allows only one writer transaction at a time, so a genuinely
    separate connection attempting a concurrent commit here would simply
    block on the outer (still-open) transaction's write lock -- there is
    no second thread free to release it, and the test would deadlock.
    Issuing the bump on the *same* connection sidesteps that: it commits
    to nothing on its own (it is still inside the outer transaction), but
    it is unconditionally visible to every later statement on that same
    connection, including the CAS ``UPDATE`` this hook was armed to
    intercept -- which is exactly the visibility a genuinely concurrent
    writer's completed commit would have produced. This reproduces
    "another writer moved the revision between this call's own read and
    its CAS UPDATE" deterministically -- no thread, no sleep -- for
    exactly the two functions in this module (`retry_item`,
    `apply_dependency_change`) whose CAS ``UPDATE`` cannot be raced by two
    ordinary sequential calls (see the module docstring)."""
    state = {"armed": True}

    def _listener(conn, cursor, statement, parameters, context, executemany) -> None:
        del cursor, parameters, context, executemany
        if not state["armed"]:
            return
        upper = statement.strip().upper()
        if upper.startswith("UPDATE") and table.upper() in upper:
            state["armed"] = False
            conn.execute(
                text(f"UPDATE {table} SET revision = revision + 1 WHERE id = :id"),
                {"id": row_id},
            )

    return _listener


# ---------------------------------------------------------------------------
# Shared fixture base (mirrors tests/test_localization_binding.py)
# ---------------------------------------------------------------------------


class _LocalizationFixtureBase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        _widen_sqlite_localization_check_constraints()

        now = datetime.now(timezone.utc)
        with tenant_session(CTX) as s:
            _ensure_workspace(s, WS_ID)
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

    def _build_ready_batch(self, tag: str) -> dict:
        """Build a Batch through the real command sequence, single US
        market, and drive it all the way through `confirm()` -- the same
        shape `tests/test_localization_binding.py`'s helper of the same
        name returns, including the exact DataSourceVersion/
        ManualFactSetVersion/TruthResolutionPolicyVersion ids the frozen
        TruthPackage depends on (needed by the `apply_dependency_change`
        binding test)."""
        winner = _build_confirmed_winner(WS_ID, CTX, tag)
        _seed_review(CTX, winner["asset_version_id"])

        with tenant_session(CTX) as s:
            mpv = _make_market_policy_version(s, f"LC{next(_MARKET_ID_COUNTER):04d}"[:10])
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
            expected_revision = s.get(LocalizationBatchRecord, batch_id).revision

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
            "market": market, "product_version_id": winner["product_version_id"],
            "offer_version_id": winner["offer_version_id"],
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

    def _batch_revision(self, batch_id: str) -> int:
        with tenant_session(CTX) as s:
            return s.get(LocalizationBatchRecord, batch_id).revision

    def _item_revision(self, item_id: str) -> int:
        with tenant_session(CTX) as s:
            return s.get(LocalizationItemRecord, item_id).revision

    # -- worker pipeline (needed only by the binding CAS test) --------

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
        """Drives a Batch all the way through a real successful finalize +
        `publish_binding()`, the same pipeline
        `tests/test_localization_binding.py` uses, so the active
        `MarketAssetBindingRecord` this returns is exactly what
        `apply_dependency_change` would find in production."""
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
        built["publication_id"] = publication.id

        with tenant_session(CTX) as s:
            first = publish_binding(
                s, CTX, item_id=built["item_id"], batch_id=built["batch_id"],
                publication_id=built["publication_id"], actor_id="user_1",
            )
        built["binding_id"] = first.binding_id
        return built


# ---------------------------------------------------------------------------
# TestCASConflicts
# ---------------------------------------------------------------------------


class TestCASConflicts(_LocalizationFixtureBase):
    """Spec §3F: "all Item transitions use CAS and a losing writer reloads
    rather than overwriting" -- and the same for the Batch and the
    MarketAssetBinding projection."""

    def test_concurrent_batch_update_loses(self) -> None:
        """Two readers observe the same Batch revision; both call
        `derive_batch_outcome()` with it. The first is the real CAS
        `UPDATE ... WHERE revision = R` winning writer. The second reuses
        the *same* pre-mutation `R` -- exactly what a concurrent caller
        that read before the first one committed would still hold -- and
        its own CAS `UPDATE` affects zero rows."""
        ready = self._build_ready_batch("cas_batch01")
        with tenant_session(CTX) as s:
            item = s.get(LocalizationItemRecord, ready["item_id"])
            item.state = "succeeded"

        # Both concurrent callers observed this revision before either wrote.
        observed_revision = self._batch_revision(ready["batch_id"])

        with tenant_session(CTX) as s:
            outcome = derive_batch_outcome(
                s, CTX, ready["batch_id"], expected_revision=observed_revision,
            )
        self.assertEqual(outcome, "succeeded")
        self.assertEqual(self._batch_revision(ready["batch_id"]), observed_revision + 1)

        with self.assertRaises(StaleRevisionError) as ctx_mgr:
            with tenant_session(CTX) as s:
                derive_batch_outcome(
                    s, CTX, ready["batch_id"], expected_revision=observed_revision,
                )
        self.assertEqual(ctx_mgr.exception.expected_revision, observed_revision)

        # The loser left no trace: revision advanced exactly once, not twice.
        self.assertEqual(self._batch_revision(ready["batch_id"]), observed_revision + 1)

    def test_concurrent_item_transition_loses(self) -> None:
        """`retry_item()`'s CAS `UPDATE` is preceded by a fresh re-read of
        `LocalizationItemRecord.state` (must be `"retry_wait"`), so two
        ordinary sequential calls can't reproduce a *revision* race: the
        second would fail on that business precondition instead, masking
        the very thing this test means to prove. The injected concurrent
        writer (see the module docstring) commits its own revision bump
        after `retry_item()` has already read `state == "retry_wait"` but
        immediately before its own CAS `UPDATE`, so the precondition still
        passes and the `UPDATE` still loses on `revision` alone."""
        ready = self._build_ready_batch("cas_item01")
        with tenant_session(CTX) as s:
            item = s.get(LocalizationItemRecord, ready["item_id"])
            item.state = "retry_wait"
        observed_revision = self._item_revision(ready["item_id"])

        listener = _inject_concurrent_revision_bump("localization_items", ready["item_id"])
        event.listen(engine, "before_cursor_execute", listener)
        try:
            with self.assertRaises(StaleRevisionError) as ctx_mgr:
                with tenant_session(CTX) as s:
                    retry_item(
                        s, CTX, batch_id=ready["batch_id"], item_id=ready["item_id"],
                        expected_item_revision=observed_revision,
                        actor_id="user_1", role="owner", admission=self.admission,
                    )
        finally:
            event.remove(engine, "before_cursor_execute", listener)

        self.assertEqual(ctx_mgr.exception.expected_revision, observed_revision)
        with tenant_session(CTX) as s:
            item = s.get(LocalizationItemRecord, ready["item_id"])
            # `tenant_session` rolls back the whole failed transaction on
            # the raised StaleRevisionError -- the injected bump lived only
            # inside that same transaction, so it is undone too. The Item
            # is exactly where it was before the race: not advanced to a
            # second Attempt.
            self.assertEqual(item.revision, observed_revision)
            self.assertEqual(item.state, "retry_wait")
            self.assertEqual(item.attempt_count, 1)

    async def test_concurrent_binding_update_loses(self) -> None:
        """`apply_dependency_change()` manages its own `expected` revision
        entirely internally -- it takes no caller-supplied revision
        parameter at all -- so no sequence of calls from a single thread
        can race it either. The injected writer bumps the row immediately
        before the function's own CAS `UPDATE` (which by then has already
        fixed the `revision` value it read moments earlier), reproducing
        the same "another writer moved the revision first" race a genuine
        second transaction would otherwise need real threading to force."""
        built = await self._run_to_finalized("cas_bind01")

        listener = _inject_concurrent_revision_bump("market_asset_bindings", built["binding_id"])
        event.listen(engine, "before_cursor_execute", listener)
        try:
            with self.assertRaises(StaleRevisionError) as ctx_mgr:
                with tenant_session(CTX) as s:
                    apply_dependency_change(
                        s, CTX, change_kind="product_version",
                        changed_version_id=built["product_version_id"],
                        actor_id="user_1",
                    )
        finally:
            event.remove(engine, "before_cursor_execute", listener)

        self.assertEqual(ctx_mgr.exception.record, "market_asset_binding")
        self.assertEqual(ctx_mgr.exception.record_id, built["binding_id"])

        with tenant_session(CTX) as s:
            binding = s.get(MarketAssetBindingRecord, built["binding_id"])
            # `tenant_session` rolls back the whole failed transaction on
            # the raised StaleRevisionError -- the injected bump lived only
            # inside that same transaction, so it is undone too, and
            # `apply_dependency_change`'s own (never-reached) truth_freshness
            # write never happened either.
            self.assertEqual(binding.revision, 1)
            self.assertEqual(binding.truth_freshness, "current")


# ---------------------------------------------------------------------------
# TestCompositeFK
# ---------------------------------------------------------------------------


class TestCompositeFK(_LocalizationFixtureBase):
    """Composite foreign keys, not merely bare-id ones, are what stop a
    child row from silently attaching to the wrong Batch, Item, Workspace
    or market (spec: composite FKs bind the shared Workspace, and several
    tables additionally bind Batch/TruthPackage/market identity into the
    same constraint) -- see `tests/test_postgres_rls.py`'s
    `test_workspace_owned_foreign_keys_are_tenant_composite` for the
    general guard this module gives Localization-specific evidence for."""

    def test_cross_batch_item_rejected(self) -> None:
        """An Item's `batch_source_id`/`batch_market_target_id` must
        belong to the exact `batch_id` the Item itself declares
        (`fk_li_batch_source`/`fk_li_batch_market_target`) -- a clone of a
        real, fully-valid Item row with only `batch_id` swapped to a
        Batch that never produced that `batch_source_id` must be
        rejected."""
        ready = self._build_ready_batch("fk_batch01")
        other = self._build_ready_batch("fk_batch02")

        with tenant_session(CTX) as s:
            item = s.get(LocalizationItemRecord, ready["item_id"])
            clone = {c.name: getattr(item, c.name) for c in LocalizationItemRecord.__table__.columns}

        clone["id"] = new_id("li")
        # A real Batch in this same Workspace, but not the one that
        # actually produced `batch_source_id`/`batch_market_target_id`.
        clone["batch_id"] = other["batch_id"]

        with self.assertRaises(IntegrityError):
            with tenant_session(CTX) as s:
                s.add(LocalizationItemRecord(**clone))

    def test_cross_source_attempt_rejected(self) -> None:
        """`fk_la_item` binds a `LocalizationAttemptRecord` to the exact
        `(item_id, batch_id)` pair `localization_items` has for that Item
        -- an Attempt claiming an `item_id` that doesn't exist under its
        declared `batch_id` must be rejected."""
        ready = self._build_ready_batch("fk_attempt01")

        with tenant_session(CTX) as s:
            attempt = s.scalar(
                select(LocalizationAttemptRecord).where(
                    LocalizationAttemptRecord.workspace_id == WS_ID,
                    LocalizationAttemptRecord.item_id == ready["item_id"],
                    LocalizationAttemptRecord.attempt_number == 1,
                )
            )
            clone = {c.name: getattr(attempt, c.name) for c in LocalizationAttemptRecord.__table__.columns}

        clone["id"] = new_id("la")
        clone["attempt_number"] = 2
        # No Item by this id exists at all under the real `batch_id`.
        clone["item_id"] = "li_never_issued"

        with self.assertRaises(IntegrityError):
            with tenant_session(CTX) as s:
                s.add(LocalizationAttemptRecord(**clone))

    def test_cross_workspace_rejected(self) -> None:
        """`fk_tp_product_version` binds `(workspace_id, product_version_id)`
        together -- a TruthPackage in Workspace A referencing a
        LibraryVersion that only exists under Workspace B must be
        rejected, even though the bare `product_version_id` is real."""
        other_ws = "ws_lcas_other"
        other_ctx = TenantContext(
            workspace_id=other_ws, role="owner", initiator_id="user_1", initiator_role="owner",
        )
        with tenant_session(other_ctx) as s:
            _ensure_workspace(s, other_ws)
            foreign_version_id = _make_version(s, other_ws, "product", "fkws01")

        now = datetime.now(timezone.utc)
        with self.assertRaises(IntegrityError):
            with tenant_session(CTX) as s:
                s.add(TruthPackageRecord(
                    id=new_id("tp"), workspace_id=WS_ID,
                    # Real LibraryVersion id -- just not one WS_ID owns.
                    product_version_id=foreign_version_id,
                    offer_version_id=foreign_version_id,
                    truth_resolution_policy_version_id=foreign_version_id,
                    truth_data_mode="real", canonical_hash=new_id("h") + "x" * 40,
                    status="full", valid_until=now + timedelta(days=1), created_at=now,
                ))

    def test_cross_market_fact_replacement_rejected(self) -> None:
        """`fk_tdfi_input_fact` binds a derived Fact's input to a real
        `truth_facts` row under the *same* `truth_package_market_id` the
        derived Fact itself belongs to -- a DerivedFactInput cannot
        "borrow" a Fact accepted for a different market within the same
        TruthPackage."""
        tag = "fk_market01"
        with tenant_session(CTX) as s:
            product_version_id = _make_version(s, WS_ID, "product", tag)
            offer_version_id = _make_version(s, WS_ID, "offer", tag)
            policy_version_id = _make_version(s, WS_ID, "truth_resolution_policy", tag)
            data_source_version_id = _make_version(s, WS_ID, "data_source", tag)

            now = datetime.now(timezone.utc)
            truth_package_id = new_id("tp")
            s.add(TruthPackageRecord(
                id=truth_package_id, workspace_id=WS_ID,
                product_version_id=product_version_id, offer_version_id=offer_version_id,
                truth_resolution_policy_version_id=policy_version_id,
                truth_data_mode="real", canonical_hash=new_id("h") + "y" * 40,
                status="full", valid_until=now + timedelta(days=1), created_at=now,
            ))
            s.flush()

            us_mpv = _make_market_policy_version(s, f"CFUS{next(_MARKET_ID_COUNTER):04d}"[:10])
            gb_mpv = _make_market_policy_version(s, f"CFGB{next(_MARKET_ID_COUNTER):04d}"[:10])

            tpm_us_id, tpm_gb_id = new_id("tpm"), new_id("tpm")
            s.add(TruthPackageMarketRecord(
                id=tpm_us_id, workspace_id=WS_ID, truth_package_id=truth_package_id,
                market_policy_version_id=us_mpv.id, market="US", readiness="ready",
                readiness_detail_json={}, created_at=now,
            ))
            s.add(TruthPackageMarketRecord(
                id=tpm_gb_id, workspace_id=WS_ID, truth_package_id=truth_package_id,
                market_policy_version_id=gb_mpv.id, market="GB", readiness="ready",
                readiness_detail_json={}, created_at=now,
            ))
            s.flush()

            attempt_id = new_id("tsa")
            s.add(TruthSourceAttemptRecord(
                id=attempt_id, workspace_id=WS_ID, truth_package_id=truth_package_id,
                truth_package_market_id=tpm_us_id, fact_type="price", scope_key="k1",
                priority_ordinal=1, data_source_version_id=data_source_version_id,
                adapter_kind="api", adapter_version="v1", resolution_rule_version="truth-rule-v1",
                truth_data_mode="real", requested_context_json={}, outcome="accepted",
                evidence_hash="e" * 64, started_at=now, completed_at=now, created_at=now,
            ))
            s.flush()

            # Accepted under the US market.
            us_fact_id = new_id("tf")
            s.add(TruthFactRecord(
                id=us_fact_id, workspace_id=WS_ID, truth_package_id=truth_package_id,
                truth_package_market_id=tpm_us_id, fact_type="price", scope_key="k1",
                accepted_source_attempt_id=attempt_id, value_json={"amount": "19.99"},
                evidence_hash="e" * 64, external_object_ref="ref",
                requested_context_json={}, observed_at=now, valid_until=now + timedelta(days=1),
                created_at=now,
            ))

            # A derivation belonging to the GB market.
            gb_derived_id = new_id("tdf")
            s.add(TruthDerivedFactRecord(
                id=gb_derived_id, workspace_id=WS_ID, truth_package_id=truth_package_id,
                truth_package_market_id=tpm_gb_id, derived_type="currency_conversion",
                scope_key="k1", value_json={}, derivation_rule_version="dr-v1", created_at=now,
            ))
            s.flush()

        with self.assertRaises(IntegrityError):
            with tenant_session(CTX) as s:
                s.add(TruthDerivedFactInputRecord(
                    id=new_id("tdfi"), workspace_id=WS_ID, truth_package_id=truth_package_id,
                    # Claims the GB derivation's market context ...
                    truth_package_market_id=tpm_gb_id,
                    derived_fact_id=gb_derived_id,
                    # ... while "replacing" it with a Fact only accepted for US.
                    input_fact_id=us_fact_id,
                ))


if __name__ == "__main__":
    unittest.main()
