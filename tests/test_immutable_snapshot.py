"""Immutable snapshot negative tests (IS-01..IS-05).

Verifies that frozen experiment versions and candidates maintain integrity
after freeze — the Version is the source of truth, not mutable copies.
"""
from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select, update

from app.adapters.persistence.database import engine, tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    ExperimentRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    PlanRecord,
    ProductRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.experiments.service import (
    ExperimentConflict,
    add_entry_v2,
    create_experiment_v2,
    freeze_experiment_v2,
)
from app.platform.canonical import _canonical_json, _sha256
from app.platform.tenancy import TenantContext
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)

CTX = TenantContext(
    workspace_id="ws_is",
    role="owner",
    initiator_id="user_is",
    request_id="req_is",
    trace_id="trace_is",
)

PROD_ID = "prod_is"


def _draft_context(**overrides: object) -> dict:
    base = {
        "market": "US",
        "currency": "USD",
        "placement": "feed",
        "product_version_id": "pv_is",
        "offer_version_id": "ov_is",
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
    ws = session.get(WorkspaceRecord, CTX.workspace_id)
    if ws is None:
        session.add(WorkspaceRecord(id=CTX.workspace_id, name="IS test"))
        session.flush()


def _seed_library_versions(session) -> None:
    for entry_id, ver_id, etype in [
        ("le_pv_is", "pv_is", "product"),
        ("le_ov_is", "ov_is", "offer"),
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
        product_version_id="pv_is", offer_version_id="ov_is",
    )


def _seed_asset_versions(session, tag: str, count: int = 2) -> list[str]:
    _ensure_workspace(session)

    if session.get(ProductRecord, PROD_ID) is None:
        session.add(ProductRecord(
            id=PROD_ID, workspace_id=CTX.workspace_id, name="P immutable",
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
    """Create and freeze an experiment, return (experiment_id, version_id)."""
    with tenant_session(CTX) as s:
        avs = _seed_asset_versions(s, tag)

    result = create_experiment_v2(CTX, product_id=PROD_ID, name=f"IS {tag}", draft_context=_draft_context())
    exp_id = result["id"]

    add_entry_v2(CTX, exp_id, asset_version_id=avs[0], expected_revision=1)
    add_entry_v2(CTX, exp_id, asset_version_id=avs[1], expected_revision=2)

    frozen = freeze_experiment_v2(CTX, exp_id, expected_revision=3)
    return exp_id, frozen["version_id"]


class ImmutableSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_workspace(s)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(
                    id=PROD_ID, workspace_id=CTX.workspace_id, name="P immutable",
                ))
                s.flush()
            _seed_library_versions(s)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def test_IS_01_frozen_snapshot_json_mutation_does_not_affect_version(self) -> None:
        """IS-01: Mutating ExperimentRecord.frozen_snapshot_json does not affect the authoritative Version."""
        exp_id, ver_id = _create_frozen_experiment("is01")

        with tenant_session(CTX) as s:
            version = s.get(ExperimentVersionRecord, ver_id)
            original_version_hash = version.frozen_snapshot_hash

            s.execute(
                update(ExperimentRecord)
                .where(ExperimentRecord.id == exp_id)
                .values(frozen_snapshot_json={"tampered": True})
            )
            s.flush()

            version_after = s.get(ExperimentVersionRecord, ver_id)
            self.assertEqual(version_after.frozen_snapshot_hash, original_version_hash)
            self.assertNotEqual(version_after.frozen_snapshot_json, {"tampered": True})

    def test_IS_02_candidate_hash_mutation_detectable(self) -> None:
        """IS-02: Mutating candidate_hash after freeze produces a detectable inconsistency."""
        exp_id, ver_id = _create_frozen_experiment("is02")

        with tenant_session(CTX) as s:
            candidates = s.scalars(
                select(ExperimentVersionCandidateRecord)
                .where(ExperimentVersionCandidateRecord.experiment_version_id == ver_id)
                .order_by(ExperimentVersionCandidateRecord.position)
            ).all()
            self.assertGreaterEqual(len(candidates), 2)

            original_hash = candidates[0].candidate_hash
            tampered_hash = "f" * 64

            s.execute(
                update(ExperimentVersionCandidateRecord)
                .where(ExperimentVersionCandidateRecord.id == candidates[0].id)
                .values(candidate_hash=tampered_hash)
            )
            s.flush()

            cand_after = s.get(ExperimentVersionCandidateRecord, candidates[0].id)
            self.assertEqual(cand_after.candidate_hash, tampered_hash)
            self.assertNotEqual(cand_after.candidate_hash, original_hash)

            content = _canonical_json({
                "position": cand_after.position,
                "asset_version_id": cand_after.asset_version_id,
                "content_sha256": cand_after.asset_version_content_sha256,
                "marketing_vars_hash": cand_after.marketing_vars_hash,
                "concepts_hash": cand_after.concepts_hash,
                "execution_mode": cand_after.execution_mode,
                "eligibility_evidence_hash": cand_after.eligibility_evidence_hash,
                "source_kind": cand_after.source_kind,
                "source_evidence_hash": cand_after.source_evidence_hash,
            })
            recomputed = _sha256(content.encode("utf-8"))
            self.assertNotEqual(tampered_hash, recomputed)
            self.assertEqual(original_hash, recomputed)

    def test_IS_03_version_snapshot_hash_is_self_consistent(self) -> None:
        """IS-03: The frozen_snapshot_hash on ExperimentVersionRecord matches the canonical hash of frozen_snapshot_json."""
        exp_id, ver_id = _create_frozen_experiment("is03")

        with tenant_session(CTX) as s:
            version = s.get(ExperimentVersionRecord, ver_id)
            snapshot_json = _canonical_json(version.frozen_snapshot_json)
            recomputed = _sha256(snapshot_json.encode("utf-8"))
            self.assertEqual(version.frozen_snapshot_hash, recomputed)

    def test_IS_04_version_record_exists_and_is_referenced(self) -> None:
        """IS-04: After freeze, ExperimentVersionRecord exists and is referenced by the experiment."""
        exp_id, ver_id = _create_frozen_experiment("is04")

        with tenant_session(CTX) as s:
            version = s.get(ExperimentVersionRecord, ver_id)
            self.assertIsNotNone(version)
            self.assertEqual(version.experiment_id, exp_id)
            self.assertEqual(version.workspace_id, CTX.workspace_id)

            experiment = s.get(ExperimentRecord, exp_id)
            self.assertEqual(experiment.state, "frozen")
            self.assertIsNotNone(experiment.frozen_snapshot_hash)
            self.assertEqual(experiment.frozen_snapshot_hash, version.frozen_snapshot_hash)

    def test_IS_05_candidates_linked_to_version(self) -> None:
        """IS-05: After freeze, candidates are linked to the version and have consistent hashes."""
        exp_id, ver_id = _create_frozen_experiment("is05")

        with tenant_session(CTX) as s:
            candidates = s.scalars(
                select(ExperimentVersionCandidateRecord)
                .where(ExperimentVersionCandidateRecord.experiment_version_id == ver_id)
                .order_by(ExperimentVersionCandidateRecord.position)
            ).all()
            self.assertEqual(len(candidates), 2)

            for cand in candidates:
                self.assertEqual(cand.workspace_id, CTX.workspace_id)
                self.assertEqual(cand.experiment_version_id, ver_id)
                self.assertIsNotNone(cand.candidate_hash)
                self.assertIsNotNone(cand.asset_version_id)
                self.assertIsNotNone(cand.asset_version_content_sha256)

                content = _canonical_json({
                    "position": cand.position,
                    "asset_version_id": cand.asset_version_id,
                    "content_sha256": cand.asset_version_content_sha256,
                    "marketing_vars_hash": cand.marketing_vars_hash,
                    "concepts_hash": cand.concepts_hash,
                    "execution_mode": cand.execution_mode,
                    "eligibility_evidence_hash": cand.eligibility_evidence_hash,
                    "source_kind": cand.source_kind,
                    "source_evidence_hash": cand.source_evidence_hash,
                })
                recomputed = _sha256(content.encode("utf-8"))
                self.assertEqual(cand.candidate_hash, recomputed)

            version = s.get(ExperimentVersionRecord, ver_id)
            cands_content = _canonical_json([
                {"id": c.id, "candidate_hash": c.candidate_hash} for c in candidates
            ])
            recomputed_cands_hash = _sha256(cands_content.encode("utf-8"))
            self.assertEqual(version.candidates_hash, recomputed_cands_hash)
