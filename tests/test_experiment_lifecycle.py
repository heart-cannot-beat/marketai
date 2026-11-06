"""Schema-2 experiment lifecycle tests (LC-01..LC-17).

Covers the full lifecycle: create → add/update/remove entry → update context
→ freeze → bind → start → judge → confirm → close, plus negative-path close
tests.
"""
from __future__ import annotations

import shutil
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.adapters.persistence.database import engine, tenant_session
from app.adapters.persistence.models import (
    AdDailyMetricRecord,
    AssetRecord,
    AssetVersionRecord,
    ContextualVerdictProjectionRecord,
    DecisionConfirmationRecord,
    DecisionProposalRecord,
    DecisionProposalStateEventRecord,
    DimensionConceptKeyRecord,
    ExperimentVersionConceptRecord,
    ExperimentAdBindingRecord,
    ExperimentDecisionRecord,
    ExperimentEligibilityProjectionRecord,
    ExperimentEntryRecord,
    ExperimentRecord,
    ExperimentStateEventRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    ExperimentVersionResolutionRecord,
    GenerationRunRecord,
    JudgementRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    MetaAccountRecord,
    MetaSyncRunRecord,
    ProductRecord,
    QualityReviewRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.generation.models import RunItemRecord
from app.experiments.eligibility import append_eligibility_event, ensure_projection
from app.experiments.service import (
    ExperimentConflict,
    ExperimentError,
    ExperimentNotFound,
    add_entry_v2,
    analyze_duplicates,
    bind_candidate_v2,
    bind_entry,
    close_experiment_v2,
    confirm_proposal_v2,
    create_experiment_v2,
    freeze_experiment_v2,
    get_experiment,
    remove_entry_v2,
    run_judgement_v2,
    start_experiment_v2,
    update_context_v2,
    update_entry_v2,
    _duplicate_group_hash,
)
from app.platform.tenancy import TenantContext
from app.platform.canonical import canonical_hash
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)

CTX = TenantContext(
    workspace_id="ws_lc",
    role="owner",
    initiator_id="user_lc",
    request_id="req_lc",
    trace_id="trace_lc",
)


def _draft_context(**overrides: object) -> dict:
    base = {
        "market": "US",
        "currency": "USD",
        "placement": "feed",
        "product_version_id": "pv_lc",
        "offer_version_id": "ov_lc",
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


PROD_ID = "prod_lc"


def _seed_asset_versions(session, tag: str, count: int = 2) -> list[str]:
    """Seed the full FK chain and return asset_version_ids."""
    _ensure_workspace(session)

    if session.get(ProductRecord, PROD_ID) is None:
        session.add(ProductRecord(
            id=PROD_ID, workspace_id=CTX.workspace_id, name="P lifecycle",
            product_profile_entry_id="le_pv_lc",
        ))
        session.flush()
    else:
        session.get(ProductRecord, PROD_ID).product_profile_entry_id = "le_pv_lc"

    plan_id = f"plan_{tag}"
    from app.adapters.persistence.models import PlanRecord
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
        concept_key_id = f"concept_{tag}_{i}"
        run_item_id = f"ri_{tag}_{i}"
        session.add(DimensionConceptKeyRecord(
            id=concept_key_id, workspace_id=CTX.workspace_id,
            scope="workspace", name=f"Concept {tag} {i}", created_by="test",
        ))
        session.add(RunItemRecord(
            id=run_item_id, workspace_id=CTX.workspace_id,
            generation_run_id=run_id, ordinal=i,
            combination_identity_json={"concepts": [{
                "dimension_key": "creative_angle",
                "concept_key_id": concept_key_id,
                "stable_dimension_signature": "creative_angle:v1",
                "value": {"index": i},
            }]},
            combination_fingerprint=canonical_hash([tag, i, "combination"]),
            stable_dimension_signature="creative_angle:v1",
            semantic_fingerprint=canonical_hash([tag, i, "semantic"]),
            execution_mode="mock", state="succeeded",
        ))
        session.flush()
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
                producing_run_item_id=run_item_id, origin_kind="generation",
                execution_mode="mock", truth_dependency_expected=False,
                truth_dependency_policy_version="truth-dependency-v1",
                truth_dependency_evidence_json={"expected": False},
                truth_dependency_evidence_hash=canonical_hash([tag, i, "truth"]),
            ))
            session.flush()
        session.add(QualityReviewRecord(
            id=f"review_{tag}_{i}", workspace_id=CTX.workspace_id,
            product_id=PROD_ID, asset_id=asset_id, asset_version_id=av_id,
            legacy_unscoped=False, review_value="approved", actor_id="reviewer",
        ))
        session.flush()
        av_ids.append(av_id)
    govern_asset_versions(
        session,
        CTX,
        product_id=PROD_ID,
        asset_version_ids=av_ids,
        tag=tag,
    )
    return av_ids


def _attach_run_item_concepts(
    session,
    asset_version_ids: list[str],
    tag: str,
    *,
    dimensions: dict[str, str],
) -> None:
    dimension_key, concept_name = next(iter(dimensions.items()))
    concept_key_id = f"concept_shared_{tag}"
    session.add(DimensionConceptKeyRecord(
        id=concept_key_id, workspace_id=CTX.workspace_id,
        scope="workspace", name=concept_name, created_by="test",
    ))
    session.flush()
    for index, asset_version_id in enumerate(asset_version_ids):
        version = session.get(AssetVersionRecord, asset_version_id)
        item = session.get(RunItemRecord, version.producing_run_item_id)
        item.combination_identity_json = {"concepts": [{
            "dimension_key": dimension_key,
            "concept_key_id": concept_key_id,
            "stable_dimension_signature": f"{dimension_key}:v1",
            "value": {"name": concept_name},
        }]}
        item.combination_fingerprint = canonical_hash([tag, index, "shared"])
        item.stable_dimension_signature = f"{dimension_key}:v1"
        version.producing_run_item_id = item.id
        version.origin_kind = "generation"
        version.execution_mode = "mock"
    session.flush()


def _seed_metrics(session, tag: str, ad_ids: list[str]) -> None:
    """Seed MetaAccount, SyncRun, and daily metrics for each ad_id."""
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


def _ensure_workspace(session) -> None:
    ws = session.get(WorkspaceRecord, CTX.workspace_id)
    if ws is None:
        session.add(WorkspaceRecord(id=CTX.workspace_id, name="LC test"))
        session.flush()


def _seed_library_versions(session) -> None:
    """Create library entries + versions for product_version_id and offer_version_id."""
    for entry_id, ver_id, etype in [
        ("le_pv_lc", "pv_lc", "product"),
        ("le_ov_lc", "ov_lc", "offer"),
    ]:
        if session.get(LibraryEntryRecord, entry_id) is None:
            session.add(LibraryEntryRecord(
                id=entry_id, workspace_id=CTX.workspace_id,
                entry_type=etype, display_name=f"Test {etype}",
            ))
            session.flush()
        if session.get(LibraryVersionRecord, ver_id) is None:
            content = {} if etype == "product" else {
                "product_version_id": "pv_lc",
                "market_scope": ["US", "GB"],
                "landing_page_url": "https://example.test/lifecycle",
                "valid_from": "2026-01-01",
                "valid_to": "2027-01-01",
            }
            session.add(LibraryVersionRecord(
                id=ver_id, workspace_id=CTX.workspace_id,
                entry_id=entry_id, entry_type=etype,
                version_no=1, content_json=content,
                content_hash=canonical_hash(content),
                state="effective", eligibility="eligible", published_by="system",
            ))
            session.flush()
    govern_product_offer_versions(
        session,
        CTX,
        product_id=PROD_ID,
        product_version_id="pv_lc",
        offer_version_id="ov_lc",
        markets=("US", "GB"),
    )


class ExperimentLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_workspace(s)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(
                    id=PROD_ID, workspace_id=CTX.workspace_id, name="P lifecycle",
                    product_profile_entry_id="le_pv_lc",
                ))
                s.flush()
            else:
                s.get(ProductRecord, PROD_ID).product_profile_entry_id = "le_pv_lc"
            _seed_library_versions(s)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    # --- LC-01 ---
    def test_LC_01_create_schema2_experiment(self) -> None:
        result = create_experiment_v2(CTX, product_id=PROD_ID, name="LC01", draft_context=_draft_context())
        self.assertEqual(result["record_schema_version"], 2)
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["state"], "draft")
        self.assertTrue(result["id"].startswith("exp_"))

    # --- LC-02 ---
    def test_LC_02_add_entry(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "lc02")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="LC02", draft_context=_draft_context())
        result = add_entry_v2(
            CTX, exp["id"], asset_version_id=avs[0],
            expected_revision=1,
        )
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["asset_version_id"], avs[0])
        self.assertEqual(result["position"], 0)

    # --- LC-03 ---
    def test_LC_03_update_entry(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "lc03")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="LC03", draft_context=_draft_context())
        entry = add_entry_v2(
            CTX, exp["id"], asset_version_id=avs[0], expected_revision=1,
        )
        result = update_entry_v2(
            CTX, exp["id"], entry["id"],
            marketing_vars={"headline": "updated"},
            expected_revision=2,
        )
        self.assertEqual(result["revision"], 3)
        self.assertEqual(result["marketing_vars"]["headline"], "updated")

    # --- LC-04 ---
    def test_LC_04_remove_entry(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "lc04")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="LC04", draft_context=_draft_context())
        entry = add_entry_v2(
            CTX, exp["id"], asset_version_id=avs[0], expected_revision=1,
        )
        result = remove_entry_v2(
            CTX, exp["id"], entry["id"], expected_revision=2,
        )
        self.assertEqual(result["revision"], 3)
        self.assertEqual(result["removed"], entry["id"])

    # --- LC-04b (#284) ---
    def test_LC_04b_removed_entry_is_soft_deleted_and_left_out_everywhere(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "lc04b")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="LC04b", draft_context=_draft_context())
        kept = add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        gone = add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        remove_entry_v2(CTX, exp["id"], gone["id"], expected_revision=3)

        # The domain database grants no DELETE: the row stays, marked removed.
        with tenant_session(CTX) as s:
            self.assertIsNotNone(s.get(ExperimentEntryRecord, gone["id"]).removed_at)
        for command in (
            lambda: update_entry_v2(CTX, exp["id"], gone["id"], marketing_vars={}, expected_revision=4),
            lambda: remove_entry_v2(CTX, exp["id"], gone["id"], expected_revision=4),
        ):
            with self.assertRaises(ExperimentNotFound):
                command()
        self.assertEqual([e["id"] for e in get_experiment(CTX, exp["id"])["entries"]], [kept["id"]])

        # Its asset version and position can be taken again, and the removed
        # row is no duplicate of the new one.
        again = add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=4)
        self.assertEqual((again["position"], again["revision"]), (1, 5))
        self.assertFalse(analyze_duplicates(CTX, exp["id"])["has_duplicates"])
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=5)
        self.assertEqual([e["id"] for e in frozen["entries"]], [kept["id"], again["id"]])
        with tenant_session(CTX) as s:
            candidates = s.scalars(select(ExperimentVersionCandidateRecord.asset_version_id).where(
                ExperimentVersionCandidateRecord.experiment_version_id == frozen["version_id"])).all()
        self.assertEqual(sorted(candidates), sorted(avs))
        # Nor can a removed entry be bound once the experiment is frozen.
        with self.assertRaises(ExperimentNotFound):
            bind_entry(CTX, exp["id"], gone["id"], "act_lc04b", "ad_lc04b")

    # --- LC-05 ---
    def test_LC_05_update_draft_context(self) -> None:
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="LC05", draft_context=_draft_context())
        new_ctx = _draft_context(market="GB", currency="GBP")
        result = update_context_v2(
            CTX, exp["id"], draft_context=new_ctx, expected_revision=1,
        )
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["draft_context"]["market"], "GB")
        self.assertEqual(result["draft_context"]["currency"], "GBP")

    # --- LC-06 ---
    def test_LC_06_freeze_experiment(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "lc06")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="LC06", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        result = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        self.assertEqual(result["state"], "frozen")
        self.assertIn("version_id", result)
        self.assertIn("candidates", result)
        self.assertEqual(len(result["candidates"]), 2)
        self.assertIsNotNone(result["frozen_snapshot_hash"])
        self.assertEqual(result["revision"], 4)

    def test_freeze_requires_and_persists_duplicate_concept_resolution(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "concept_allow")
            _attach_run_item_concepts(
                s, avs, "concept_allow", dimensions={"hook": "social-proof"}
            )
        exp = create_experiment_v2(
            CTX,
            product_id=PROD_ID,
            name="Concept resolution",
            draft_context=_draft_context(),
        )
        first = add_entry_v2(
            CTX, exp["id"], asset_version_id=avs[0], expected_revision=1
        )
        second = add_entry_v2(
            CTX, exp["id"], asset_version_id=avs[1], expected_revision=2
        )

        with self.assertRaisesRegex(ExperimentConflict, "concept_unresolved") as failure:
            freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        group_hash = str(failure.exception).split(":", 1)[1]

        frozen = freeze_experiment_v2(
            CTX,
            exp["id"],
            expected_revision=3,
            concept_resolution_plan=[{
                "duplicate_group_hash": group_hash,
                "action": "allow_controlled_duplicate",
                "reason": "Intentional hook control",
                "source_entry_ids": [first["id"], second["id"]],
            }],
        )
        self.assertEqual([row["execution_mode"] for row in frozen["candidates"]], ["mock", "mock"])
        with tenant_session(CTX) as s:
            concepts = s.scalars(
                select(ExperimentVersionConceptRecord).where(
                    ExperimentVersionConceptRecord.experiment_version_id
                    == frozen["version_id"]
                )
            ).all()
            resolutions = s.scalars(
                select(ExperimentVersionResolutionRecord).where(
                    ExperimentVersionResolutionRecord.experiment_version_id
                    == frozen["version_id"]
                )
            ).all()
        self.assertEqual(len(concepts), 2)
        self.assertEqual(len(resolutions), 1)
        self.assertEqual(resolutions[0].action, "allow_controlled_duplicate")
        self.assertEqual(
            concepts[0].duplicate_group_hash,
            _duplicate_group_hash(concepts[0].dimension_key, concepts[0].concept_key_id),
        )
        self.assertEqual(concepts[0].duplicate_group_hash, concepts[1].duplicate_group_hash)

    def test_freeze_rejects_stale_duplicate_group_membership(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "concept_stale")
            _attach_run_item_concepts(
                s, avs, "concept_stale", dimensions={"angle": "urgency"}
            )
        exp = create_experiment_v2(
            CTX,
            product_id=PROD_ID,
            name="Stale concept plan",
            draft_context=_draft_context(),
        )
        first = add_entry_v2(
            CTX, exp["id"], asset_version_id=avs[0], expected_revision=1
        )
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        with self.assertRaisesRegex(ExperimentConflict, "concept_unresolved") as failure:
            freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        group_hash = str(failure.exception).split(":", 1)[1]

        with self.assertRaisesRegex(ExperimentConflict, "concept_resolution_stale"):
            freeze_experiment_v2(
                CTX,
                exp["id"],
                expected_revision=3,
                concept_resolution_plan=[{
                    "duplicate_group_hash": group_hash,
                    "action": "allow_controlled_duplicate",
                    "reason": "Incomplete membership must fail",
                    "source_entry_ids": [first["id"]],
                }],
            )

    def test_freeze_projects_exact_mode_and_rejects_mixed_modes(self) -> None:
        with tenant_session(CTX) as s:
            sandbox_assets = _seed_asset_versions(s, "mode_sandbox")
            for asset_version_id in sandbox_assets:
                s.get(AssetVersionRecord, asset_version_id).execution_mode = "sandbox"
        sandbox_exp = create_experiment_v2(
            CTX,
            product_id=PROD_ID,
            name="Sandbox modes",
            draft_context=_draft_context(),
        )
        add_entry_v2(
            CTX, sandbox_exp["id"], asset_version_id=sandbox_assets[0], expected_revision=1
        )
        add_entry_v2(
            CTX, sandbox_exp["id"], asset_version_id=sandbox_assets[1], expected_revision=2
        )
        frozen = freeze_experiment_v2(CTX, sandbox_exp["id"], expected_revision=3)
        self.assertEqual(
            [candidate["execution_mode"] for candidate in frozen["candidates"]],
            ["sandbox", "sandbox"],
        )

        with tenant_session(CTX) as s:
            mixed_assets = _seed_asset_versions(s, "mode_mixed")
            s.get(AssetVersionRecord, mixed_assets[0]).execution_mode = "sandbox"
            s.get(AssetVersionRecord, mixed_assets[1]).execution_mode = "mock"
        mixed_exp = create_experiment_v2(
            CTX,
            product_id=PROD_ID,
            name="Mixed modes",
            draft_context=_draft_context(),
        )
        add_entry_v2(
            CTX, mixed_exp["id"], asset_version_id=mixed_assets[0], expected_revision=1
        )
        add_entry_v2(
            CTX, mixed_exp["id"], asset_version_id=mixed_assets[1], expected_revision=2
        )
        with self.assertRaisesRegex(ExperimentConflict, "execution_mode_mixed"):
            freeze_experiment_v2(CTX, mixed_exp["id"], expected_revision=3)

    # --- LC-07 ---
    def test_LC_07_bind_candidate(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "lc07")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="LC07", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        cand_id = frozen["candidates"][0]["id"]
        result = bind_candidate_v2(
            CTX, exp["id"], cand_id,
            ad_account_id="act_lc07_0", ad_id="ad_lc07_0",
            expected_revision=frozen["revision"],
        )
        self.assertIn("id", result)
        self.assertEqual(result["candidate_id"], cand_id)
        self.assertEqual(result["ad_account_id"], "act_lc07_0")

    # --- LC-08 ---
    def test_LC_08_start_experiment(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "lc08")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="LC08", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        rev = frozen["revision"]
        for cand in frozen["candidates"]:
            bind_candidate_v2(
                CTX, exp["id"], cand["id"],
                ad_account_id=f"act_lc08_{cand['position']}",
                ad_id=f"ad_lc08_{cand['position']}",
                expected_revision=rev,
            )
        result = start_experiment_v2(CTX, exp["id"], expected_revision=rev)
        self.assertEqual(result["state"], "running")
        self.assertIsNotNone(result["started_at"])
        self.assertEqual(result["revision"], rev + 1)

    # --- helper: build a running experiment ---
    def _build_running(self, tag: str) -> tuple[dict, list[str]]:
        """Returns (experiment_dict, [ad_id_0, ad_id_1])."""
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, tag)
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name=f"EXP-{tag}", draft_context=_draft_context())
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
                ad_id=ad_id,
                expected_revision=rev,
            )
        running = start_experiment_v2(CTX, exp["id"], expected_revision=rev)
        return running, ad_ids

    # --- LC-09 ---
    def test_LC_09_run_judgement(self) -> None:
        running, ad_ids = self._build_running("lc09")
        with tenant_session(CTX) as s:
            _seed_metrics(s, "lc09", ad_ids)
        result = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )
        self.assertIn("judgement", result)
        self.assertIn("proposal_id", result)
        self.assertIsNotNone(result["proposal_id"])
        self.assertEqual(result.get("proposal_state"), "created")

    # --- LC-10 ---
    def test_LC_10_confirm_proposal(self) -> None:
        running, ad_ids = self._build_running("lc10")
        with tenant_session(CTX) as s:
            _seed_metrics(s, "lc10", ad_ids)
        j_result = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )
        result = confirm_proposal_v2(
            CTX, running["id"], j_result["proposal_id"],
            expected_proposal_sequence=1,
        )
        self.assertIn("confirmation_id", result)
        self.assertIsInstance(result["decisions"], list)
        self.assertGreater(len(result["decisions"]), 0)
        with tenant_session(CTX) as s:
            projs = s.scalars(
                select(ContextualVerdictProjectionRecord).where(
                    ContextualVerdictProjectionRecord.experiment_id == running["id"],
                )
            ).all()
            self.assertGreater(len(projs), 0)

    # --- LC-11 ---
    def test_LC_11_close_completed(self) -> None:
        running, ad_ids = self._build_running("lc11")
        with tenant_session(CTX) as s:
            _seed_metrics(s, "lc11", ad_ids)
        j_result = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )
        result = close_experiment_v2(
            CTX, running["id"], expected_revision=running["revision"],
            closure_kind="completed",
        )
        self.assertEqual(result["state"], "closed")
        self.assertIsNotNone(result["closed_at"])

    # --- LC-12 ---
    def test_LC_12_close_abandoned_before_start(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "lc12")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="LC12", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        result = close_experiment_v2(
            CTX, exp["id"], expected_revision=frozen["revision"],
            closure_kind="abandoned_before_start",
        )
        self.assertEqual(result["state"], "closed")

    # --- LC-13 ---
    def test_LC_13_close_aborted_from_frozen(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "lc13")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="LC13", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        result = close_experiment_v2(
            CTX, exp["id"], expected_revision=frozen["revision"],
            closure_kind="aborted",
        )
        self.assertEqual(result["state"], "closed")

    def test_LC_13b_close_aborted_from_running(self) -> None:
        running, _ = self._build_running("lc13b")
        result = close_experiment_v2(
            CTX, running["id"], expected_revision=running["revision"],
            closure_kind="aborted",
        )
        self.assertEqual(result["state"], "closed")

    # --- LC-14 ---
    def test_LC_14_close_invalidated(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "lc14")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="LC14", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        version_id = frozen["version_id"]
        with tenant_session(CTX) as s:
            append_eligibility_event(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp["id"],
                experiment_version_id=version_id,
                upstream_event_type="product_version_withdrawn",
                upstream_resource_type="library_version",
                upstream_resource_id="pv_withdrawn",
                upstream_event_id=new_id("upev"),
                evidence={"reason": "withdrawn"},
            )
        result = close_experiment_v2(
            CTX, exp["id"], expected_revision=frozen["revision"],
            closure_kind="invalidated",
        )
        self.assertEqual(result["state"], "closed")

    # --- LC-15 ---
    def test_LC_15_close_completed_no_judgements(self) -> None:
        running, _ = self._build_running("lc15")
        with self.assertRaises(ExperimentConflict) as cm:
            close_experiment_v2(
                CTX, running["id"], expected_revision=running["revision"],
                closure_kind="completed",
            )
        self.assertIn("no_judgements", str(cm.exception))

    # --- LC-16 ---
    def test_LC_16_close_invalidated_no_irreversible(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "lc16")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="LC16", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        with self.assertRaises(ExperimentConflict) as cm:
            close_experiment_v2(
                CTX, exp["id"], expected_revision=frozen["revision"],
                closure_kind="invalidated",
            )
        self.assertIn("no_active_invalidation", str(cm.exception))

    # --- LC-17 ---
    def test_LC_17_close_abandoned_from_running(self) -> None:
        running, _ = self._build_running("lc17")
        with self.assertRaises(ExperimentConflict) as cm:
            close_experiment_v2(
                CTX, running["id"], expected_revision=running["revision"],
                closure_kind="abandoned_before_start",
            )
        self.assertIn("Expected state frozen", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
