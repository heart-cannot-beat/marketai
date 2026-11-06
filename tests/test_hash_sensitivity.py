"""Hash determinism and snapshot integrity tests (§5.3.4–§5.3.6).

Covers HD-02..HD-07, HD-09 (remaining hash determinism from §5.3.4),
and SI-01..SI-04 (immutable snapshot integrity from §5.3.6).

HD-01, HD-08, and ECH-01..07 are already covered in test_canonical_json.py.
"""
from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.adapters.persistence.database import engine, tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    PlanRecord,
    ProductRecord,
    WorkspaceRecord,
    new_id,
)
from app.experiments.service import (
    add_entry_v2,
    create_experiment_v2,
    freeze_experiment_v2,
)
from app.platform.canonical import _canonical_bytes, _canonical_json, _sha256, canonical_hash
from app.platform.tenancy import TenantContext
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)


CTX = TenantContext(
    workspace_id="ws_hs",
    role="owner",
    initiator_id="user_hs",
    request_id="req_hs",
    trace_id="trace_hs",
)

PROD_ID = "prod_hs"


def _draft_context(**overrides: object) -> dict:
    base = {
        "market": "US",
        "currency": "USD",
        "placement": "feed",
        "product_version_id": "pv_hs",
        "offer_version_id": "ov_hs",
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
        session.add(WorkspaceRecord(id=CTX.workspace_id, name="HS test"))
        session.flush()


def _seed_library_versions(session) -> None:
    for entry_id, ver_id, etype in [
        ("le_pv_hs", "pv_hs", "product"),
        ("le_ov_hs", "ov_hs", "offer"),
    ]:
        if session.get(LibraryEntryRecord, entry_id) is None:
            session.add(LibraryEntryRecord(
                id=entry_id, workspace_id=CTX.workspace_id,
                entry_type=etype, display_name=f"Test {etype}",
            ))
            session.flush()
        if session.get(LibraryVersionRecord, ver_id) is None:
            session.add(LibraryVersionRecord(
                id=ver_id, workspace_id=CTX.workspace_id,
                entry_id=entry_id, entry_type=etype,
                version_no=1, content_json={}, content_hash="c" * 64,
                state="effective", published_by="system",
            ))
            session.flush()
    govern_product_offer_versions(
        session, CTX, product_id=PROD_ID,
        product_version_id="pv_hs", offer_version_id="ov_hs",
    )


def _seed_asset_versions(session, tag: str, count: int = 2) -> list[str]:
    _ensure_workspace(session)
    if session.get(ProductRecord, PROD_ID) is None:
        session.add(ProductRecord(
            id=PROD_ID, workspace_id=CTX.workspace_id, name="P hash",
        ))
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


def _create_frozen_experiment(tag: str) -> tuple[str, str]:
    with tenant_session(CTX) as s:
        _seed_asset_versions(s, tag)

    result = create_experiment_v2(CTX, product_id=PROD_ID, name=f"HS {tag}", draft_context=_draft_context())
    exp_id = result["id"]
    add_entry_v2(CTX, exp_id, asset_version_id=f"av_{tag}_0", expected_revision=1)
    add_entry_v2(CTX, exp_id, asset_version_id=f"av_{tag}_1", expected_revision=2)
    frozen = freeze_experiment_v2(CTX, exp_id, expected_revision=3)
    return exp_id, frozen["version_id"]


# ---------------------------------------------------------------------------
# §5.3.4 Hash determinism (HD-02..HD-07, HD-09)
# HD-01 and HD-08 are covered in test_canonical_json.py
# ---------------------------------------------------------------------------


class TestHashDeterminism(unittest.TestCase):
    """Pure unit tests: compute hashes twice, assert equality."""

    def test_HD_02_truth_dependency_hash_no_package(self) -> None:
        """HD-02: Truth-dependency hash for (v1-bootstrap, False, None, 'grandfathered')."""
        inputs = {
            "determination_type": "v1-bootstrap",
            "truth_dependency_expected": False,
            "truth_package_id": None,
            "reason": "grandfathered_pre_truth_system",
        }
        h1 = _sha256(_canonical_bytes(inputs))
        h2 = _sha256(_canonical_bytes(inputs))
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_HD_03_truth_dependency_hash_with_package(self) -> None:
        """HD-03: Truth-dependency hash for (v1-bootstrap, True, 'tp-001', 'grandfathered')."""
        inputs = {
            "determination_type": "v1-bootstrap",
            "truth_dependency_expected": True,
            "truth_package_id": "tp-001",
            "reason": "grandfathered_pre_truth_system",
        }
        h1 = _sha256(_canonical_bytes(inputs))
        h2 = _sha256(_canonical_bytes(inputs))
        self.assertEqual(h1, h2)

    def test_HD_04_qc_evidence_hash(self) -> None:
        """HD-04: QC evidence hash from (rule_id, rule_version, check_type, severity, outcome, evidence_obj)."""
        inputs = {
            "rule_id": "qc_rule_001",
            "rule_version": "1.0",
            "check_type": "image_safety",
            "severity": "blocking",
            "outcome": "failed_blocking",
            "evidence_obj": {"score": 85, "threshold": 90, "model": "safety-v2"},
        }
        h1 = _sha256(_canonical_bytes(inputs))
        h2 = _sha256(_canonical_bytes(inputs))
        self.assertEqual(h1, h2)

    def test_HD_05_eligibility_context_hash(self) -> None:
        """HD-05: eligibility_context_hash from 4-input tuple is deterministic."""
        inputs = [
            "frozen_snapshot_hash_aaa",
            "version_proj_head_bbb",
            [["cand_1", "cand_hash_1"], ["cand_2", "cand_hash_2"]],
            "recheck_evidence_hash_ddd",
        ]
        h1 = _sha256(_canonical_bytes(inputs))
        h2 = _sha256(_canonical_bytes(inputs))
        self.assertEqual(h1, h2)

    def test_HD_06_proposal_hash(self) -> None:
        """HD-06: proposal_hash from (eligibility_context_hash, judgement_hash, entries_canonical)."""
        inputs = {
            "eligibility_context_hash": "a" * 64,
            "judgement_hash": "b" * 64,
            "entries_canonical": [
                {"candidate_id": "c1", "verdict": "win"},
                {"candidate_id": "c2", "verdict": "lose"},
            ],
        }
        h1 = _sha256(_canonical_bytes(inputs))
        h2 = _sha256(_canonical_bytes(inputs))
        self.assertEqual(h1, h2)

    def test_HD_07_context_hash(self) -> None:
        """HD-07: context_hash from (experiment_id, offer_version_id, market, placement, lpe_hash)."""
        inputs = {
            "experiment_id": "exp_001",
            "offer_version_id": "ov_001",
            "market": "US",
            "placement": "feed",
            "lpe_hash": "e" * 64,
        }
        h1 = _sha256(_canonical_bytes(inputs))
        h2 = _sha256(_canonical_bytes(inputs))
        self.assertEqual(h1, h2)

    def test_HD_09_candidate_hash_chain(self) -> None:
        """HD-09: candidate_hash chain is deterministic for fixed candidate inputs."""
        cand_input = {
            "asset_version_id": "av_001",
            "content_sha256": "0" * 64,
            "marketing_vars_hash": "1" * 64,
            "concepts_hash": "2" * 64,
            "execution_mode": "mock",
        }
        h1 = _sha256(_canonical_bytes(cand_input))
        h2 = _sha256(_canonical_bytes(cand_input))
        self.assertEqual(h1, h2)

        chain_input = [
            {"id": "cand_1", "hash": h1},
            {"id": "cand_2", "hash": "3" * 64},
        ]
        ch1 = _sha256(_canonical_bytes(chain_input))
        ch2 = _sha256(_canonical_bytes(chain_input))
        self.assertEqual(ch1, ch2)

    def test_HD_02_different_inputs_produce_different_hashes(self) -> None:
        """Complementary: different truth-dependency inputs → different hashes."""
        no_pkg = {"determination_type": "v1-bootstrap", "truth_dependency_expected": False, "truth_package_id": None, "reason": "grandfathered_pre_truth_system"}
        with_pkg = {"determination_type": "v1-bootstrap", "truth_dependency_expected": True, "truth_package_id": "tp-001", "reason": "grandfathered_pre_truth_system"}
        self.assertNotEqual(_sha256(_canonical_bytes(no_pkg)), _sha256(_canonical_bytes(with_pkg)))


# ---------------------------------------------------------------------------
# §5.3.6 Immutable snapshot integrity (SI-01..SI-04)
# ---------------------------------------------------------------------------


class TestSnapshotIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_workspace(s)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(
                    id=PROD_ID, workspace_id=CTX.workspace_id, name="P hash",
                ))
                s.flush()
            _seed_library_versions(s)
        cls.exp_id, cls.ver_id = _create_frozen_experiment("si")

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def test_SI_01_projection_columns_equal_snapshot_values(self) -> None:
        """SI-01: Normalized integrity projection columns equal values extracted from frozen_snapshot_json."""
        with tenant_session(CTX) as s:
            version = s.get(ExperimentVersionRecord, self.ver_id)
            snapshot = version.frozen_snapshot_json

            self.assertEqual(version.frozen_snapshot_json["schema_version"], 2)
            self.assertEqual(version.frozen_snapshot_json["experiment_id"], self.exp_id)
            self.assertEqual(version.frozen_snapshot_json["version_id"], self.ver_id)
            self.assertEqual(version.frozen_snapshot_json["candidates_hash"], version.candidates_hash)
            self.assertEqual(version.frozen_snapshot_json["resolutions_hash"], version.concept_resolutions_hash)
            self.assertEqual(version.frozen_snapshot_json["statistical_plan_hash"], version.statistical_plan_hash)
            self.assertEqual(version.frozen_snapshot_json["audience_hash"], version.audience_snapshot_hash)

    def test_SI_02_frozen_snapshot_hash_matches_canonical(self) -> None:
        """SI-02: frozen_snapshot_hash equals _sha256(_canonical_bytes(frozen_snapshot_json))."""
        with tenant_session(CTX) as s:
            version = s.get(ExperimentVersionRecord, self.ver_id)
            recomputed = _sha256(_canonical_bytes(version.frozen_snapshot_json))
            self.assertEqual(version.frozen_snapshot_hash, recomputed)

    def test_SI_03_candidates_hash_matches_ordered_candidate_hashes(self) -> None:
        """SI-03: candidates_hash equals _sha256 of ordered candidate hashes."""
        with tenant_session(CTX) as s:
            version = s.get(ExperimentVersionRecord, self.ver_id)
            candidates = s.scalars(
                select(ExperimentVersionCandidateRecord)
                .where(ExperimentVersionCandidateRecord.experiment_version_id == self.ver_id)
                .order_by(ExperimentVersionCandidateRecord.position)
            ).all()
            self.assertGreaterEqual(len(candidates), 2)

            cands_content = _canonical_json([
                {"id": c.id, "candidate_hash": c.candidate_hash} for c in candidates
            ])
            recomputed = _sha256(cands_content.encode("utf-8"))
            self.assertEqual(version.candidates_hash, recomputed)

    def test_SI_04_resolutions_hash_matches_resolution_records(self) -> None:
        """SI-04: concept_resolutions_hash equals _sha256 of resolution records (empty case)."""
        with tenant_session(CTX) as s:
            version = s.get(ExperimentVersionRecord, self.ver_id)
            recomputed = canonical_hash([])
            self.assertEqual(version.concept_resolutions_hash, recomputed)


if __name__ == "__main__":
    unittest.main()
