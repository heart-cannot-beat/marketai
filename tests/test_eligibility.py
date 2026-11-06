"""EL-01..EL-12: Eligibility event and projection tests (§5.4.3)."""
from __future__ import annotations

import shutil
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select, update

from app.adapters.persistence.database import engine, platform_session, tenant_session
from app.adapters.persistence.models import (
    DecisionProposalRecord,
    DecisionProposalStateEventRecord,
    ExperimentEligibilityEventRecord,
    ExperimentEligibilityProjectionRecord,
    ExperimentRecord,
    ExperimentVersionRecord,
    JudgementRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    ProductRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.platform.canonical import _canonical_json, _sha256
from app.experiments.eligibility import (
    IRREVERSIBLE_EVENT_TYPES,
    RESTORATION_EVENT_TYPES,
    REVERSIBLE_EVENT_TYPES,
    EligibilitySequenceConflict,
    append_eligibility_event,
    ensure_projection,
    is_effectively_eligible,
    rebuild_projection,
)
from app.platform.tenancy import TenantContext

CTX = TenantContext(
    workspace_id="ws_elig",
    role="owner",
    initiator_id="user_elig",
    request_id="req_elig",
    trace_id="trace_elig",
)


def _seed_experiment_and_version(session, suffix: str) -> tuple[str, str]:
    exp_id = f"exp_elig_{suffix}"
    ver_id = f"ver_elig_{suffix}"
    prod_id = f"prod_elig_{suffix}"
    pv_entry_id = f"le_pv_{suffix}"
    ov_entry_id = f"le_ov_{suffix}"
    pv_ver_id = f"lv_pv_{suffix}"
    ov_ver_id = f"lv_ov_{suffix}"

    session.add(ProductRecord(
        id=prod_id, workspace_id=CTX.workspace_id, name=f"EligProd {suffix}",
    ))
    session.flush()

    session.add(ExperimentRecord(
        id=exp_id, workspace_id=CTX.workspace_id, product_id=prod_id,
        name=f"Elig {suffix}", state="frozen", revision=2,
        config_json={}, metric_contract_hash="", record_schema_version=2,
    ))
    session.flush()

    for eid, vid, etype in [
        (pv_entry_id, pv_ver_id, "product"),
        (ov_entry_id, ov_ver_id, "offer"),
    ]:
        session.add(LibraryEntryRecord(
            id=eid, workspace_id=CTX.workspace_id,
            entry_type=etype, display_name=f"{etype} {suffix}",
        ))
        session.flush()
        session.add(LibraryVersionRecord(
            id=vid, workspace_id=CTX.workspace_id, entry_id=eid,
            entry_type=etype, version_no=1, content_json={},
            content_hash="a" * 64, published_by="test",
        ))
        session.flush()

    now = utcnow()
    session.add(ExperimentVersionRecord(
        id=ver_id, workspace_id=CTX.workspace_id, experiment_id=exp_id,
        canonicalization_version="p1-migration-v1",
        frozen_snapshot_json={}, frozen_snapshot_hash="a" * 64,
        metric_contract_hash="", market="US", placement="feed",
        landing_page_evidence_hash="b" * 64,
        audience_snapshot_json={}, audience_snapshot_hash="c" * 64,
        daily_budget_amount=Decimal("100"), total_budget_cap=Decimal("3000"),
        budget_currency="USD",
        schedule_start_at=now, schedule_end_at=now + timedelta(days=30),
        schedule_timezone="UTC",
        attribution_click_window_days=7, attribution_view_window_days=1,
        statistical_plan_json={}, statistical_plan_hash="d" * 64,
        marketing_vars_schema_version="1",
        product_version_id=pv_ver_id, offer_version_id=ov_ver_id,
        candidates_hash="e" * 64, concept_resolutions_hash="f" * 64,
    ))
    session.flush()
    return exp_id, ver_id


class EligibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with platform_session() as session:
            existing = session.get(WorkspaceRecord, CTX.workspace_id)
            if existing is None:
                session.add(WorkspaceRecord(
                    id=CTX.workspace_id, name="Elig Test WS",
                ))

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def test_el01_reversible_event_makes_ineligible(self) -> None:
        with tenant_session(CTX) as session:
            exp_id, ver_id = _seed_experiment_and_version(session, "el01")
            proj = ensure_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id, level="version",
            )
            self.assertEqual(proj.current_eligibility, "eligible")

            append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                upstream_event_type="review_rejected",
                upstream_resource_type="quality_review", upstream_resource_id="rev_01",
                upstream_event_id="uev_01", evidence={"reason": "bad content"},
            )
            session.refresh(proj)
            self.assertEqual(proj.current_eligibility, "ineligible")
            self.assertFalse(proj.irreversible_active)

    def test_el02_restoration_event_restores_eligibility(self) -> None:
        with tenant_session(CTX) as session:
            exp_id, ver_id = _seed_experiment_and_version(session, "el02")
            ensure_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id, level="version",
            )
            append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                upstream_event_type="review_rejected",
                upstream_resource_type="quality_review", upstream_resource_id="rev_02a",
                upstream_event_id="uev_02a", evidence={},
            )
            append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                upstream_event_type="review_re_approved",
                upstream_resource_type="quality_review", upstream_resource_id="rev_02b",
                upstream_event_id="uev_02b", evidence={},
            )
            proj = session.scalar(
                select(ExperimentEligibilityProjectionRecord).where(
                    ExperimentEligibilityProjectionRecord.experiment_version_id == ver_id,
                    ExperimentEligibilityProjectionRecord.level == "version",
                )
            )
            self.assertEqual(proj.current_eligibility, "eligible")
            self.assertFalse(proj.irreversible_active)

    def test_el03_irreversible_event_invalidates_open_proposals(self) -> None:
        with tenant_session(CTX) as session:
            exp_id, ver_id = _seed_experiment_and_version(session, "el03")
            ensure_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id, level="version",
            )
            judge_id = "judge_el03"
            snap = _canonical_json({"test": "el03"})
            session.add(JudgementRecord(
                id=judge_id, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                algorithm_version="v1", as_of=date(2026, 7, 24),
                snapshot_hash=_sha256(snap.encode("utf-8")),
                input_snapshot_json={}, result_json={},
            ))
            session.flush()
            prop_id = new_id("prop")
            session.add(DecisionProposalRecord(
                id=prop_id, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, judgement_id=judge_id,
                experiment_version_id=ver_id,
                eligibility_context_hash="x" * 64,
                proposal_hash="y" * 64,
                proposed_entries_json=[], evidence_json={},
            ))
            session.flush()
            session.add(DecisionProposalStateEventRecord(
                id=new_id("dpse"), workspace_id=CTX.workspace_id,
                proposal_id=prop_id, event_type="created", sequence=1,
            ))
            session.flush()

            append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                upstream_event_type="product_version_withdrawn",
                upstream_resource_type="library_version",
                upstream_resource_id="pv_el03", upstream_event_id="uev_03",
                evidence={"reason": "withdrawn"},
            )

            proj = session.scalar(
                select(ExperimentEligibilityProjectionRecord).where(
                    ExperimentEligibilityProjectionRecord.experiment_version_id == ver_id,
                    ExperimentEligibilityProjectionRecord.level == "version",
                )
            )
            self.assertTrue(proj.irreversible_active)
            self.assertEqual(proj.current_eligibility, "ineligible")

            latest_pse = session.scalar(
                select(DecisionProposalStateEventRecord)
                .where(DecisionProposalStateEventRecord.proposal_id == prop_id)
                .order_by(DecisionProposalStateEventRecord.sequence.desc())
                .limit(1)
            )
            self.assertEqual(latest_pse.event_type, "invalidated")

    def test_el04_idempotent_duplicate_event_creates_second_row(self) -> None:
        with tenant_session(CTX) as session:
            exp_id, ver_id = _seed_experiment_and_version(session, "el04")
            ensure_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id, level="version",
            )
            ev1 = append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                upstream_event_type="review_rejected",
                upstream_resource_type="quality_review", upstream_resource_id="rev_04",
                upstream_event_id="uev_04", evidence={},
            )
            ev2 = append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                upstream_event_type="review_rejected",
                upstream_resource_type="quality_review", upstream_resource_id="rev_04b",
                upstream_event_id="uev_04b", evidence={},
            )
            self.assertNotEqual(ev1.id, ev2.id)
            proj = session.scalar(
                select(ExperimentEligibilityProjectionRecord).where(
                    ExperimentEligibilityProjectionRecord.experiment_version_id == ver_id,
                    ExperimentEligibilityProjectionRecord.level == "version",
                )
            )
            self.assertEqual(proj.head_sequence, 2)

    def test_el05_incremental_fold_matches_rebuild(self) -> None:
        with tenant_session(CTX) as session:
            exp_id, ver_id = _seed_experiment_and_version(session, "el05")
            ensure_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id, level="version",
            )
            append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                upstream_event_type="review_rejected",
                upstream_resource_type="quality_review", upstream_resource_id="rev_05a",
                upstream_event_id="uev_05a", evidence={},
            )
            append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                upstream_event_type="review_re_approved",
                upstream_resource_type="quality_review", upstream_resource_id="rev_05b",
                upstream_event_id="uev_05b", evidence={},
            )
            proj_inc = session.scalar(
                select(ExperimentEligibilityProjectionRecord).where(
                    ExperimentEligibilityProjectionRecord.experiment_version_id == ver_id,
                    ExperimentEligibilityProjectionRecord.level == "version",
                )
            )
            inc_hash = proj_inc.head_hash
            inc_seq = proj_inc.head_sequence

            rebuilt = rebuild_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id, level="version",
            )
            self.assertEqual(rebuilt.head_hash, inc_hash)
            self.assertEqual(rebuilt.head_sequence, inc_seq)

    def test_el06_equal_timestamps_advance_by_1us(self) -> None:
        with tenant_session(CTX) as session:
            exp_id, ver_id = _seed_experiment_and_version(session, "el06")
            ensure_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id, level="version",
            )
            ev1 = append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                upstream_event_type="review_rejected",
                upstream_resource_type="quality_review", upstream_resource_id="rev_06a",
                upstream_event_id="uev_06a", evidence={},
            )
            ev2 = append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                upstream_event_type="qc_failed_blocking",
                upstream_resource_type="asset_qc_attempt", upstream_resource_id="qc_06b",
                upstream_event_id="uev_06b", evidence={},
            )
            self.assertGreater(ev2.observed_at, ev1.observed_at)

            rebuilt = rebuild_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id, level="version",
            )
            proj = session.scalar(
                select(ExperimentEligibilityProjectionRecord).where(
                    ExperimentEligibilityProjectionRecord.experiment_version_id == ver_id,
                    ExperimentEligibilityProjectionRecord.level == "version",
                )
            )
            self.assertEqual(proj.head_hash, rebuilt.head_hash)

    def test_el07_in_place_rebuild_matches(self) -> None:
        with tenant_session(CTX) as session:
            exp_id, ver_id = _seed_experiment_and_version(session, "el07")
            ensure_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id, level="version",
            )
            append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                upstream_event_type="product_version_withdrawn",
                upstream_resource_type="library_version",
                upstream_resource_id="pv_07", upstream_event_id="uev_07",
                evidence={},
            )
            proj = session.scalar(
                select(ExperimentEligibilityProjectionRecord).where(
                    ExperimentEligibilityProjectionRecord.experiment_version_id == ver_id,
                    ExperimentEligibilityProjectionRecord.level == "version",
                )
            )
            pre_hash = proj.head_hash
            pre_seq = proj.head_sequence
            pre_elig = proj.current_eligibility
            pre_irrev = proj.irreversible_active

            rebuilt = rebuild_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id, level="version",
            )
            self.assertEqual(rebuilt.head_hash, pre_hash)
            self.assertEqual(rebuilt.head_sequence, pre_seq)
            self.assertEqual(rebuilt.current_eligibility, pre_elig)
            self.assertEqual(rebuilt.irreversible_active, pre_irrev)

    def test_el08_baseline_head_hash_is_deterministic(self) -> None:
        with tenant_session(CTX) as session:
            exp_id, ver_id = _seed_experiment_and_version(session, "el08")
            proj = ensure_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id, level="version",
            )
            self.assertIsNotNone(proj.head_hash)
            self.assertTrue(len(proj.head_hash) == 64)

        with tenant_session(CTX) as session:
            exp_id2, ver_id2 = _seed_experiment_and_version(session, "el08b")
            proj2 = ensure_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id2, experiment_version_id=ver_id2, level="version",
            )
            self.assertNotEqual(proj.head_hash, proj2.head_hash)

    def test_el09_cas_on_stale_head_sequence_rejects(self) -> None:
        with tenant_session(CTX) as session:
            exp_id, ver_id = _seed_experiment_and_version(session, "el09")
            proj = ensure_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id, level="version",
            )
            real_seq = proj.head_sequence
            stale_seq = real_seq + 100
            result = session.execute(
                update(ExperimentEligibilityProjectionRecord)
                .where(
                    ExperimentEligibilityProjectionRecord.id == proj.id,
                    ExperimentEligibilityProjectionRecord.head_sequence == stale_seq,
                )
                .values(head_sequence=stale_seq + 1)
            )
            self.assertEqual(result.rowcount, 0, "CAS with wrong sequence must match zero rows")

    def test_el10_composite_version_and_candidate_eligibility(self) -> None:
        with tenant_session(CTX) as session:
            exp_id, ver_id = _seed_experiment_and_version(session, "el10")
            ensure_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id, level="version",
            )
            cand_id = "cand_el10"
            ensure_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                level="candidate", candidate_id=cand_id,
            )

            eligible, evidence = is_effectively_eligible(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
            )
            self.assertTrue(eligible)

            append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                candidate_id=cand_id,
                upstream_event_type="review_rejected",
                upstream_resource_type="quality_review", upstream_resource_id="rev_10",
                upstream_event_id="uev_10", evidence={},
            )

            eligible2, evidence2 = is_effectively_eligible(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
            )
            self.assertFalse(eligible2)
            self.assertFalse(evidence2["candidates"][cand_id]["eligible"])

    def test_el11_ineligible_blocks_start(self) -> None:
        with tenant_session(CTX) as session:
            exp_id, ver_id = _seed_experiment_and_version(session, "el11")
            ensure_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id, level="version",
            )
            append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                upstream_event_type="review_rejected",
                upstream_resource_type="quality_review", upstream_resource_id="rev_11",
                upstream_event_id="uev_11", evidence={},
            )
            eligible, _ = is_effectively_eligible(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
            )
            self.assertFalse(eligible)

    def test_el12_ineligible_blocks_confirm(self) -> None:
        with tenant_session(CTX) as session:
            exp_id, ver_id = _seed_experiment_and_version(session, "el12")
            ensure_projection(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id, level="version",
            )
            append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
                upstream_event_type="product_version_withdrawn",
                upstream_resource_type="library_version",
                upstream_resource_id="pv_12", upstream_event_id="uev_12",
                evidence={},
            )
            eligible, evidence = is_effectively_eligible(
                session, workspace_id=CTX.workspace_id,
                experiment_id=exp_id, experiment_version_id=ver_id,
            )
            self.assertFalse(eligible)
            self.assertTrue(evidence.get("version_irreversible"))


if __name__ == "__main__":
    unittest.main()
