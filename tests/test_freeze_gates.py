"""Authoritative, fail-closed freeze gate tests (FG-01..FG-20)."""
from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.adapters.persistence.database import engine, tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    DimensionConceptKeyRecord,
    ExperimentEntryRecord,
    ExperimentRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    PlanRecord,
    ProductRecord,
    QualityReviewRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.generation.models import AssetQCAttemptRecord, RunItemRecord
from app.experiments.service import (
    ExperimentConflict,
    ExperimentError,
    add_entry_v2,
    create_experiment_v2,
    freeze_experiment_v2,
    validate_live_launch_eligibility,
)
from app.platform.canonical import _canonical_json, _sha256
from app.platform.tenancy import TenantContext

CTX = TenantContext(
    workspace_id="ws_fg",
    role="owner",
    initiator_id="user_fg",
    request_id="req_fg",
    trace_id="trace_fg",
)
PROD_ID = "prod_fg"


def _draft_context(**overrides: object) -> dict:
    base = {
        "market": "US",
        "currency": "USD",
        "placement": "feed",
        "product_version_id": "pv_fg",
        "offer_version_id": "ov_fg",
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


def _ensure_workspace(session) -> None:
    if session.get(WorkspaceRecord, CTX.workspace_id) is None:
        session.add(WorkspaceRecord(id=CTX.workspace_id, name="FG test"))
        session.flush()


def _ensure_product(session) -> None:
    _ensure_workspace(session)
    if session.get(ProductRecord, PROD_ID) is None:
        session.add(ProductRecord(
            id=PROD_ID, workspace_id=CTX.workspace_id, name="P freeze-gates",
            product_profile_entry_id="le_pv_fg",
        ))
        session.flush()
    else:
        session.get(ProductRecord, PROD_ID).product_profile_entry_id = "le_pv_fg"


def _seed_library_versions(session) -> None:
    for entry_id, ver_id, etype in [
        ("le_pv_fg", "pv_fg", "product"),
        ("le_ov_fg", "ov_fg", "offer"),
    ]:
        if session.get(LibraryEntryRecord, entry_id) is None:
            session.add(LibraryEntryRecord(
                id=entry_id, workspace_id=CTX.workspace_id,
                entry_type=etype, display_name=f"Test {etype}",
            ))
            session.flush()
        if session.get(LibraryVersionRecord, ver_id) is None:
            content = {} if etype == "product" else {
                "product_version_id": "pv_fg",
                "market_scope": ["US"],
                "landing_page_url": "https://example.test/offer",
                "valid_from": "2026-01-01",
                "valid_to": "2027-01-01",
                "source_market_snapshot": {
                    "market": "US",
                    "placement": "feed",
                    "locale": "en-US",
                    "country": "US",
                    "currency": "USD",
                },
            }
            session.add(LibraryVersionRecord(
                id=ver_id, workspace_id=CTX.workspace_id,
                entry_id=entry_id, entry_type=etype,
                version_no=1, content_json=content,
                content_hash=_sha256(_canonical_json(content).encode("utf-8")),
                state="effective", eligibility="eligible", published_by="system",
            ))
            session.flush()
    offer = session.get(LibraryVersionRecord, "ov_fg")
    content = dict(offer.content_json or {})
    content["source_market_snapshot"] = {
        "market": "US",
        "placement": "feed",
        "locale": "en-US",
        "country": "US",
        "currency": "USD",
    }
    offer.content_json = content
    offer.content_hash = _sha256(_canonical_json(content).encode("utf-8"))
    session.flush()


def _seed_asset_versions(session, tag: str, count: int = 2) -> list[str]:
    _ensure_product(session)
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
        concept_key_id = f"concept_{tag}_{i}"
        if session.get(DimensionConceptKeyRecord, concept_key_id) is None:
            session.add(DimensionConceptKeyRecord(
                id=concept_key_id,
                workspace_id=CTX.workspace_id,
                scope="workspace",
                name=f"Concept {tag} {i}",
                created_by="test",
            ))
            session.flush()
        run_item_id = f"ritem_{tag}_{i}"
        if session.get(RunItemRecord, run_item_id) is None:
            session.add(RunItemRecord(
                id=run_item_id,
                workspace_id=CTX.workspace_id,
                generation_run_id=run_id,
                ordinal=i,
                combination_identity_json={
                    "concepts": [{
                        "dimension_key": "creative_angle",
                        "concept_key_id": concept_key_id,
                        "stable_dimension_signature": "creative_angle:v1",
                        "value": {"name": f"Concept {tag} {i}"},
                    }],
                },
                combination_fingerprint=_sha256(f"combo:{tag}:{i}".encode()),
                stable_dimension_signature="creative_angle:v1",
                semantic_fingerprint=_sha256(f"semantic:{tag}:{i}".encode()),
                execution_mode="mock",
                state="succeeded",
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
                producing_run_item_id=run_item_id,
                origin_kind="generation",
                execution_mode="mock",
                truth_dependency_expected=False,
                truth_dependency_policy_version="truth-dependency-v1",
                truth_dependency_evidence_json={"expected": False},
                truth_dependency_evidence_hash=_sha256(
                    _canonical_json({"expected": False}).encode("utf-8")
                ),
            ))
            session.flush()
        default_qc_id = f"qc_default_{tag}_{i}"
        if session.get(AssetQCAttemptRecord, default_qc_id) is None:
            session.add(AssetQCAttemptRecord(
                id=default_qc_id,
                workspace_id=CTX.workspace_id,
                asset_version_id=av_id,
                run_item_id=run_item_id,
                qc_policy_version="qc-v1",
                rule_set_version="rules-v1",
                input_hash=f"{i:0>64x}",
                execution_mode="mock",
                state="passed",
                attempt_sequence=1,
            ))
            session.flush()
        review_id = f"review_{tag}_{i}"
        if session.get(QualityReviewRecord, review_id) is None:
            session.add(QualityReviewRecord(
                id=review_id,
                workspace_id=CTX.workspace_id,
                product_id=PROD_ID,
                asset_id=asset_id,
                asset_version_id=av_id,
                review_value="approved",
                legacy_unscoped=False,
                actor_id="reviewer",
            ))
            session.flush()
        av_ids.append(av_id)
    return av_ids


def _create_and_add_entries(tag: str, count: int = 2) -> tuple[str, int]:
    """Create an experiment and add `count` entries. Return (experiment_id, revision)."""
    with tenant_session(CTX) as s:
        avs = _seed_asset_versions(s, tag, count)
    exp = create_experiment_v2(CTX, product_id=PROD_ID, name=f"FG-{tag}", draft_context=_draft_context())
    rev = 1
    for av in avs:
        result = add_entry_v2(CTX, exp["id"], asset_version_id=av, expected_revision=rev)
        rev = result["revision"]
    return exp["id"], rev


def _entry_asset_versions(session, experiment_id: str) -> list[AssetVersionRecord]:
    entries = session.scalars(
        select(ExperimentEntryRecord)
        .where(ExperimentEntryRecord.experiment_id == experiment_id)
        .order_by(ExperimentEntryRecord.position)
    ).all()
    return [session.get(AssetVersionRecord, entry.asset_version_id) for entry in entries]


class FreezeGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_product(s)
            _seed_library_versions(s)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    # --- FG-01: Gate 1 review not approved ---
    def test_FG_01_rejected_review_blocks_freeze(self) -> None:
        exp_id, rev = _create_and_add_entries("fg01")
        with tenant_session(CTX) as s:
            av = _entry_asset_versions(s, exp_id)[0]
            review = s.scalar(select(QualityReviewRecord).where(
                QualityReviewRecord.asset_version_id == av.id,
            ))
            review.review_value = "rejected"
        with self.assertRaisesRegex(ExperimentConflict, "review_not_approved"):
            freeze_experiment_v2(CTX, exp_id, rev)

    # --- FG-02: Gate 1 no scoped review exists ---
    def test_FG_02_no_review_rows_block_freeze(self) -> None:
        exp_id, rev = _create_and_add_entries("fg02")
        with tenant_session(CTX) as s:
            av = _entry_asset_versions(s, exp_id)[0]
            review = s.scalar(select(QualityReviewRecord).where(
                QualityReviewRecord.asset_version_id == av.id,
            ))
            review.legacy_unscoped = True
        with self.assertRaisesRegex(ExperimentConflict, "review_not_approved"):
            freeze_experiment_v2(CTX, exp_id, rev)

    # --- FG-03: Gate 1 NULL review_value ---
    def test_FG_03_null_review_blocks_freeze(self) -> None:
        exp_id, rev = _create_and_add_entries("fg03")
        with tenant_session(CTX) as s:
            av = _entry_asset_versions(s, exp_id)[0]
            review = s.scalar(select(QualityReviewRecord).where(
                QualityReviewRecord.asset_version_id == av.id,
            ))
            review.review_value = None
        with self.assertRaisesRegex(ExperimentConflict, "review_unknown"):
            freeze_experiment_v2(CTX, exp_id, rev)

    # --- FG-04: Gate 1 total order tie uses id DESC ---
    def test_FG_04_review_tie_breaking_is_deterministic(self) -> None:
        exp_id, rev = _create_and_add_entries("fg04")
        with tenant_session(CTX) as s:
            av = _entry_asset_versions(s, exp_id)[0]
            approved = s.scalar(select(QualityReviewRecord).where(
                QualityReviewRecord.asset_version_id == av.id,
            ))
            s.add(QualityReviewRecord(
                id=f"zz_{approved.id}", workspace_id=CTX.workspace_id,
                product_id=PROD_ID, asset_id=av.asset_id,
                asset_version_id=av.id, review_value="rejected",
                legacy_unscoped=False, actor_id="reviewer",
                created_at=approved.created_at,
            ))
        with self.assertRaisesRegex(ExperimentConflict, "review_not_approved"):
            freeze_experiment_v2(CTX, exp_id, rev)

    # --- FG-05: Gate 1 legacy_unscoped exclusion ---
    def test_FG_05_legacy_unscoped_review_is_ignored(self) -> None:
        exp_id, rev = _create_and_add_entries("fg05")
        with tenant_session(CTX) as s:
            av = _entry_asset_versions(s, exp_id)[0]
            review = s.scalar(select(QualityReviewRecord).where(
                QualityReviewRecord.asset_version_id == av.id,
            ))
            review.legacy_unscoped = True
        with self.assertRaisesRegex(ExperimentConflict, "review_not_approved"):
            freeze_experiment_v2(CTX, exp_id, rev)

    # --- FG-06: Gate 1 exact AssetVersion exclusion ---
    def test_FG_06_wrong_av_review_does_not_authorize(self) -> None:
        exp_id, rev = _create_and_add_entries("fg06")
        with tenant_session(CTX) as s:
            avs = _entry_asset_versions(s, exp_id)
            review = s.scalar(select(QualityReviewRecord).where(
                QualityReviewRecord.asset_version_id == avs[0].id,
            ))
            review.legacy_unscoped = True
            s.add(QualityReviewRecord(
                id="review_fg06_wrong", workspace_id=CTX.workspace_id,
                product_id=PROD_ID, asset_id=avs[0].asset_id,
                asset_version_id=avs[1].id, review_value="approved",
                legacy_unscoped=False, actor_id="reviewer",
            ))
        with self.assertRaisesRegex(ExperimentConflict, "review_not_approved"):
            freeze_experiment_v2(CTX, exp_id, rev)

    # --- FG-07: Gate 2 QC unresolved blocking ---
    def test_FG_07_qc_unresolved_blocks_freeze(self) -> None:
        exp_id, rev = _create_and_add_entries("fg07")
        with tenant_session(CTX) as s:
            av = _entry_asset_versions(s, exp_id)[0]
            s.add(AssetQCAttemptRecord(
                id="qc_fg07", workspace_id=CTX.workspace_id,
                asset_version_id=av.id, run_item_id=av.producing_run_item_id,
                qc_policy_version="qc-v1", rule_set_version="rules-v1",
                input_hash=av.content_sha256, execution_mode="mock",
                state="failed_blocking", attempt_sequence=2,
            ))
        with self.assertRaisesRegex(ExperimentConflict, "qc_blocked"):
            freeze_experiment_v2(CTX, exp_id, rev)

    # --- FG-08: Gate 2 QC advisory ---
    def test_FG_08_qc_advisory_freeze_proceeds(self) -> None:
        exp_id, rev = _create_and_add_entries("fg08")
        with tenant_session(CTX) as s:
            av = _entry_asset_versions(s, exp_id)[0]
            s.add(AssetQCAttemptRecord(
                id="qc_fg08", workspace_id=CTX.workspace_id,
                asset_version_id=av.id, run_item_id=av.producing_run_item_id,
                qc_policy_version="qc-v1", rule_set_version="rules-v1",
                input_hash=av.content_sha256, execution_mode="mock",
                state="failed_nonblocking", attempt_sequence=2,
            ))
        result = freeze_experiment_v2(CTX, exp_id, rev)
        self.assertEqual(result["state"], "frozen")

    # --- FG-09: Gate 3 QC active attempt ---
    def test_FG_09_qc_active_attempt_blocks_freeze(self) -> None:
        exp_id, rev = _create_and_add_entries("fg09")
        with tenant_session(CTX) as s:
            av = _entry_asset_versions(s, exp_id)[0]
            s.add(AssetQCAttemptRecord(
                id="qc_fg09", workspace_id=CTX.workspace_id,
                asset_version_id=av.id, run_item_id=av.producing_run_item_id,
                qc_policy_version="qc-v1", rule_set_version="rules-v1",
                input_hash=av.content_sha256, execution_mode="mock",
                state="running", attempt_sequence=2,
            ))
        with self.assertRaisesRegex(ExperimentConflict, "qc_in_progress"):
            freeze_experiment_v2(CTX, exp_id, rev)

    # --- FG-10: Gate 12 execution_mode uniformity (all "mock" currently) ---
    def test_FG_10_execution_mode_uniform_mock(self) -> None:
        exp_id, rev = _create_and_add_entries("fg10")
        result = freeze_experiment_v2(CTX, exp_id, rev)
        self.assertEqual(result["state"], "frozen")
        with tenant_session(CTX) as s:
            cands = s.scalars(
                select(ExperimentVersionCandidateRecord).where(
                    ExperimentVersionCandidateRecord.experiment_version_id == result["version_id"],
                )
            ).all()
            modes = {c.execution_mode for c in cands}
            self.assertEqual(modes, {"mock"})

    # --- FG-11: Gate 13 truth-dependency NULL ---
    def test_FG_11_truth_null_blocks_freeze(self) -> None:
        exp_id, rev = _create_and_add_entries("fg11")
        with tenant_session(CTX) as s:
            av = _entry_asset_versions(s, exp_id)[0]
            av.truth_dependency_expected = None
        with self.assertRaisesRegex(ExperimentConflict, "truth_dependency_missing"):
            freeze_experiment_v2(CTX, exp_id, rev)

    # --- FG-12: Gate 13 missing required truth package ---
    def test_FG_12_stale_truth_blocks_freeze(self) -> None:
        exp_id, rev = _create_and_add_entries("fg12")
        with tenant_session(CTX) as s:
            av = _entry_asset_versions(s, exp_id)[0]
            av.truth_dependency_expected = True
        with self.assertRaisesRegex(ExperimentConflict, "truth_dependency_missing"):
            freeze_experiment_v2(CTX, exp_id, rev)

    # --- FG-13: Gate 13 truth expected=false has explicit evidence ---
    def test_FG_13_truth_false_freeze_proceeds(self) -> None:
        exp_id, rev = _create_and_add_entries("fg13")
        result = freeze_experiment_v2(CTX, exp_id, rev)
        self.assertEqual(result["state"], "frozen")

    # --- FG-14: Gate 14 withdrawal ---
    def test_FG_14_withdrawal_blocks_freeze(self) -> None:
        exp_id, rev = _create_and_add_entries("fg14")
        with tenant_session(CTX) as s:
            offer = s.get(LibraryVersionRecord, "ov_fg")
            offer.state = "withdrawn"
            offer.eligibility = "blocked"
        try:
            with self.assertRaisesRegex(ExperimentConflict, "offer_version_ineligible"):
                freeze_experiment_v2(CTX, exp_id, rev)
        finally:
            with tenant_session(CTX) as s:
                offer = s.get(LibraryVersionRecord, "ov_fg")
                offer.state = "effective"
                offer.eligibility = "eligible"

    # --- FG-15: Gate 15 concept resolution ---
    def test_FG_15_unresolved_duplicate_concept_blocks_freeze(self) -> None:
        exp_id, rev = _create_and_add_entries("fg15")
        with tenant_session(CTX) as s:
            avs = _entry_asset_versions(s, exp_id)
            first = s.get(RunItemRecord, avs[0].producing_run_item_id)
            second = s.get(RunItemRecord, avs[1].producing_run_item_id)
            second.combination_identity_json = first.combination_identity_json
        with self.assertRaisesRegex(ExperimentConflict, "concept_unresolved"):
            freeze_experiment_v2(CTX, exp_id, rev)

    # --- FG-16: Gate 16 candidate count < 2 ---
    def test_FG_16_insufficient_candidates(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "fg16", count=1)
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="FG16", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        with self.assertRaises(ExperimentConflict) as cm:
            freeze_experiment_v2(CTX, exp["id"], 2)
        self.assertIn("insufficient_candidates", str(cm.exception))

    # --- FG-17: Gate 16 candidate count > 100 ---
    def test_FG_17_candidate_limit_exceeded(self) -> None:
        # add_entry_v2 enforces >= 100 limit before freeze is reached.
        # Verify that the 101st add_entry is rejected.
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "fg17", count=101)
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="FG17", draft_context=_draft_context())
        rev = 1
        added = 0
        for av in avs:
            try:
                result = add_entry_v2(CTX, exp["id"], asset_version_id=av, expected_revision=rev)
                rev = result["revision"]
                added += 1
            except ExperimentConflict as e:
                self.assertIn("candidate_limit_exceeded", str(e))
                break
        self.assertEqual(added, 100)

    # --- FG-18: Gate 16 candidate count 2-100 (valid) ---
    def test_FG_18_valid_candidate_count(self) -> None:
        exp_id, rev = _create_and_add_entries("fg18", count=5)
        result = freeze_experiment_v2(CTX, exp_id, rev)
        self.assertEqual(result["state"], "frozen")
        self.assertEqual(len(result["candidates"]), 5)

    # --- FG-19: All basic gates pass ---
    def test_FG_19_all_gates_pass_snapshot_verified(self) -> None:
        exp_id, rev = _create_and_add_entries("fg19")
        result = freeze_experiment_v2(CTX, exp_id, rev)
        self.assertEqual(result["state"], "frozen")
        self.assertIsNotNone(result.get("frozen_snapshot_hash"))
        self.assertTrue(len(result["frozen_snapshot_hash"]) == 64)
        with tenant_session(CTX) as s:
            version = s.scalar(
                select(ExperimentVersionRecord).where(
                    ExperimentVersionRecord.experiment_id == exp_id,
                )
            )
            self.assertIsNotNone(version)
            self.assertEqual(version.frozen_snapshot_hash, result["frozen_snapshot_hash"])
            cands = s.scalars(
                select(ExperimentVersionCandidateRecord).where(
                    ExperimentVersionCandidateRecord.experiment_version_id == version.id,
                )
            ).all()
            self.assertEqual(len(cands), 2)
            for c in cands:
                self.assertTrue(len(c.candidate_hash) == 64)

    def test_source_market_snapshot_is_atomic_and_not_budget_currency(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "fg_source_market")
            offer = s.get(LibraryVersionRecord, "ov_fg")
            content = dict(offer.content_json)
            content["source_market_snapshot"] = {
                "market": "DE",
                "placement": "feed",
                "locale": "de-DE",
                "country": "DE",
                "currency": "EUR",
            }
            offer.content_json = content
            offer.content_hash = _sha256(_canonical_json(content).encode("utf-8"))
        exp = create_experiment_v2(
            CTX,
            product_id=PROD_ID,
            name="FG source market",
            draft_context=_draft_context(currency="USD"),
        )
        try:
            add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
            add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
            frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
            with tenant_session(CTX) as s:
                version = s.get(ExperimentVersionRecord, frozen["version_id"])
                self.assertEqual(version.frozen_snapshot_json["budget"]["currency"], "USD")
                self.assertEqual(version.frozen_snapshot_json["source_market_snapshot"], {
                    "market": "DE",
                    "placement": "feed",
                    "locale": "de-DE",
                    "country": "DE",
                    "currency": "EUR",
                })
        finally:
            with tenant_session(CTX) as s:
                offer = s.get(LibraryVersionRecord, "ov_fg")
                content = dict(offer.content_json)
                content["source_market_snapshot"] = {
                    "market": "US", "placement": "feed", "locale": "en-US",
                    "country": "US", "currency": "USD",
                }
                offer.content_json = content
                offer.content_hash = _sha256(_canonical_json(content).encode("utf-8"))

    def test_partial_source_market_snapshot_is_rejected_without_inference(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "fg_source_partial")
            offer = s.get(LibraryVersionRecord, "ov_fg")
            content = dict(offer.content_json)
            content["source_market_snapshot"] = {"market": "US"}
            offer.content_json = content
            offer.content_hash = _sha256(_canonical_json(content).encode("utf-8"))
        exp = create_experiment_v2(
            CTX,
            product_id=PROD_ID,
            name="FG source partial",
            draft_context=_draft_context(),
        )
        try:
            add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
            add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
            with self.assertRaisesRegex(ExperimentConflict, "source_market_context_incomplete"):
                freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        finally:
            with tenant_session(CTX) as s:
                offer = s.get(LibraryVersionRecord, "ov_fg")
                content = dict(offer.content_json)
                content["source_market_snapshot"] = {
                    "market": "US", "placement": "feed", "locale": "en-US",
                    "country": "US", "currency": "USD",
                }
                offer.content_json = content
                offer.content_hash = _sha256(_canonical_json(content).encode("utf-8"))

    # --- FG-20: Advisory at draft ---
    def test_FG_20_advisory_at_draft_creation(self) -> None:
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="FG20", draft_context=_draft_context())
        self.assertEqual(exp["state"], "draft")
        self.assertEqual(exp["record_schema_version"], 2)

    # --- Additional: freeze idempotency ---
    def test_FG_freeze_idempotent(self) -> None:
        exp_id, rev = _create_and_add_entries("fg_idem")
        result1 = freeze_experiment_v2(CTX, exp_id, rev)
        self.assertEqual(result1["state"], "frozen")
        result2 = freeze_experiment_v2(CTX, exp_id, rev + 1)
        self.assertEqual(result2["state"], "frozen")
        self.assertEqual(result1["frozen_snapshot_hash"], result2["frozen_snapshot_hash"])

    def test_launch_rechecks_review_after_freeze(self) -> None:
        exp_id, rev = _create_and_add_entries("fg_launch_live")
        frozen = freeze_experiment_v2(CTX, exp_id, rev)
        with tenant_session(CTX) as s:
            candidates = validate_live_launch_eligibility(
                s, CTX, experiment_id=exp_id,
                experiment_version_id=frozen["version_id"],
            )
            current = s.scalar(select(QualityReviewRecord).where(
                QualityReviewRecord.asset_version_id == candidates[0].asset_version_id,
                QualityReviewRecord.legacy_unscoped.is_(False),
            ))
            s.add(QualityReviewRecord(
                id=new_id("review"), workspace_id=CTX.workspace_id,
                product_id=PROD_ID, asset_id=current.asset_id,
                asset_version_id=current.asset_version_id,
                review_value="rejected", legacy_unscoped=False,
                actor_id="reviewer", supersedes_review_id=current.id,
            ))
        with tenant_session(CTX) as s:
            with self.assertRaisesRegex(
                ExperimentConflict, "launch_candidate_ineligible.*review_not_approved",
            ):
                validate_live_launch_eligibility(
                    s, CTX, experiment_id=exp_id,
                    experiment_version_id=frozen["version_id"],
                )

    # --- Additional: freeze from wrong state ---
    def test_FG_freeze_from_wrong_state(self) -> None:
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="FG_wrong_state", draft_context=_draft_context())
        with tenant_session(CTX) as s:
            s.execute(
                ExperimentRecord.__table__.update()
                .where(ExperimentRecord.id == exp["id"])
                .values(state="running")
            )
        with self.assertRaises(ExperimentConflict):
            freeze_experiment_v2(CTX, exp["id"], 1)

    # --- Additional: zero entries ---
    def test_FG_zero_entries(self) -> None:
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="FG_zero", draft_context=_draft_context())
        with self.assertRaises(ExperimentConflict) as cm:
            freeze_experiment_v2(CTX, exp["id"], 1)
        self.assertIn("insufficient_candidates", str(cm.exception))
