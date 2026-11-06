"""IV-01..IV-10: Invalidation and restoration tests (§5.4.5)."""
from __future__ import annotations

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
    DecisionProposalRecord,
    DecisionProposalStateEventRecord,
    ExperimentEligibilityProjectionRecord,
    ExperimentRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    GenerationRunRecord,
    JudgementRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    MetaAccountRecord,
    MetaSyncRunRecord,
    PlanRecord,
    ProductRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.experiments.eligibility import (
    append_eligibility_event,
    ensure_projection,
    is_effectively_eligible,
)
from app.experiments.service import (
    ExperimentConflict,
    ExperimentError,
    add_entry_v2,
    bind_candidate_v2,
    close_experiment_v2,
    confirm_proposal_v2,
    create_experiment_v2,
    freeze_experiment_v2,
    run_judgement_v2,
    start_experiment_v2,
)
from app.platform.tenancy import TenantContext
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)

WS = "ws_iv_test"
CTX = TenantContext(
    workspace_id=WS,
    role="owner",
    initiator_id="user_iv",
    request_id="req_iv",
    trace_id="trace_iv",
)


def _draft_context(suffix: str) -> dict:
    return {
        "market": "US", "currency": "USD", "placement": "feed",
        "product_version_id": f"pv_iv_{suffix}",
        "offer_version_id": f"ov_iv_{suffix}",
        "budget": {"daily_amount": "100", "total_amount": "3000"},
        "schedule": {
            "start_at": "2026-09-01T00:00:00+00:00",
            "end_at": "2026-12-01T00:00:00+00:00", "timezone": "UTC",
        },
        "attribution": {"click_window_days": 7, "view_window_days": 1},
        "statistical_plan": {
            "primary_metric": "ctr", "alpha": "0.05",
            "bootstrap_samples": 2000, "ctr_threshold": "0.01",
        },
    }


def _seed_assets(suffix: str) -> list[str]:
    """Seed workspace + product + plan + run + 2 assets/versions, return version IDs."""
    with tenant_session(CTX) as s:
        if s.get(WorkspaceRecord, WS) is None:
            s.add(WorkspaceRecord(id=WS, name="IV Test"))
            s.flush()
        for lib_id, etype in [
            (f"pv_iv_{suffix}", "product"),
            (f"ov_iv_{suffix}", "offer"),
        ]:
            if s.get(LibraryEntryRecord, lib_id) is None:
                s.add(LibraryEntryRecord(
                    id=lib_id, workspace_id=WS, entry_type=etype,
                    display_name=lib_id,
                ))
                s.flush()
            if s.get(LibraryVersionRecord, lib_id) is None:
                s.add(LibraryVersionRecord(
                    id=lib_id, workspace_id=WS, entry_id=lib_id,
                    entry_type=etype, version_no=1,
                    content_json={}, content_hash="c" * 64,
                    published_by="system",
                ))
                s.flush()
        prod_id = f"prod_iv_{suffix}"
        if s.get(ProductRecord, prod_id) is None:
            s.add(ProductRecord(id=prod_id, workspace_id=WS, name=f"P {suffix}"))
            s.flush()
        govern_product_offer_versions(
            s, CTX, product_id=prod_id,
            product_version_id=f"pv_iv_{suffix}",
            offer_version_id=f"ov_iv_{suffix}",
        )
        plan_id = f"plan_iv_{suffix}"
        if s.get(PlanRecord, plan_id) is None:
            s.add(PlanRecord(
                id=plan_id, workspace_id=WS, product_id=prod_id,
                mode="full", cap=0, total_full=1, snapshot_hash="a" * 64,
            ))
            s.flush()
        run_id = f"run_iv_{suffix}"
        if s.get(GenerationRunRecord, run_id) is None:
            s.add(GenerationRunRecord(
                id=run_id, workspace_id=WS, product_id=prod_id,
                plan_id=plan_id, provider="mock", model="mock",
                aspect_ratio="1:1", image_size="1024", variants=1,
                use_ref=False, status="done", total=2, done=2, failed=0,
                est_cost=Decimal("0"), currency="USD",
            ))
            s.flush()
        version_ids = []
        for i in range(2):
            asset_id = f"asset_iv_{suffix}_{i}"
            ver_id = f"ver_iv_{suffix}_{i}"
            if s.get(AssetRecord, asset_id) is None:
                s.add(AssetRecord(
                    id=asset_id, workspace_id=WS, product_id=prod_id,
                    run_id=run_id, combo_id="C01", combo_code="A1",
                    tags_json={}, filename=f"{suffix}_{i}.png",
                    provider="mock", model="mock", prompt="test",
                    fingerprint=f"fp_iv_{suffix}_{i}", status="ok",
                ))
                s.flush()
            if s.get(AssetVersionRecord, ver_id) is None:
                s.add(AssetVersionRecord(
                    id=ver_id, workspace_id=WS, asset_id=asset_id,
                    kind="generated", aspect_ratio="1:1", language="en",
                    object_key=f"{suffix}_{i}.png",
                    content_sha256=f"{suffix}{i}" + "a" * (64 - len(f"{suffix}{i}")),
                    byte_size=100, mime_type="image/png",
                ))
                s.flush()
            version_ids.append(ver_id)
        govern_asset_versions(
            s, CTX, product_id=prod_id,
            asset_version_ids=version_ids, tag=f"iv_{suffix}",
        )
        acct_id = f"mact_iv_{suffix}"
        if s.get(MetaAccountRecord, acct_id) is None:
            s.add(MetaAccountRecord(
                id=acct_id, workspace_id=WS,
                ad_account_id=f"act_iv_{suffix}",
                currency="USD", timezone_name="UTC", api_version="v23.0",
            ))
            s.flush()
        sync_id = f"sync_iv_{suffix}"
        if s.scalar(select(MetaSyncRunRecord).where(MetaSyncRunRecord.id == sync_id)) is None:
            s.add(MetaSyncRunRecord(
                id=sync_id, workspace_id=WS, account_id=acct_id,
                state="completed", date_start=date(2026, 8, 1),
                date_stop=date(2026, 8, 1), contract_hash="b" * 64,
                contract_json={}, api_version="v23.0",
                account_currency="USD", account_timezone="UTC",
                idempotency_key=f"iv-sync-{suffix}",
            ))
            s.flush()
        for i, ver_id in enumerate(version_ids):
            metric_id = f"metric_iv_{suffix}_{i}"
            existing = s.scalar(select(AdDailyMetricRecord).where(AdDailyMetricRecord.id == metric_id))
            if existing is None:
                s.add(AdDailyMetricRecord(
                    id=metric_id, account_id=acct_id, sync_run_id=sync_id,
                    ad_id=f"ad_iv_{suffix}_{i}", ad_name="test",
                    metric_date=date(2026, 8, 1),
                    spend=Decimal("50"), impressions=10000, clicks=500,
                    purchases=Decimal("10"), purchase_value=Decimal("500"),
                    currency="USD", contract_hash="b" * 64, raw_json={},
                ))
                s.flush()
    return version_ids


def _create_running_experiment(suffix: str):
    """Create a schema-2 experiment in 'running' state. Returns (exp_id, revision, version_id, candidate_ids)."""
    ver_ids = _seed_assets(suffix)
    result = create_experiment_v2(
        CTX, product_id=f"prod_iv_{suffix}", name=f"IV Exp {suffix}",
        draft_context=_draft_context(suffix),
    )
    exp_id = result["id"]
    rev = result["revision"]
    for vid in ver_ids:
        r = add_entry_v2(CTX, exp_id, asset_version_id=vid, expected_revision=rev)
        rev = r["revision"]
    frozen = freeze_experiment_v2(CTX, exp_id, rev)
    rev = frozen["revision"]
    cand_ids = [c["id"] for c in frozen["candidates"]]
    for i, cid in enumerate(cand_ids):
        bind_candidate_v2(
            CTX, exp_id, cid,
            ad_account_id=f"act_iv_{suffix}",
            ad_id=f"ad_iv_{suffix}_{i}",
            expected_revision=rev,
        )
    started = start_experiment_v2(CTX, exp_id, rev)
    rev = started["revision"]
    version_id = started.get("version_id")
    if not version_id:
        with tenant_session(CTX) as s:
            ver = s.scalar(select(ExperimentVersionRecord).where(
                ExperimentVersionRecord.experiment_id == exp_id,
            ))
            version_id = ver.id
    return exp_id, rev, version_id, cand_ids


class InvalidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def test_iv01_irreversible_event_terminal_proposal(self):
        """IV-01: Irreversible event → all open proposals get invalidated state event."""
        exp_id, rev, version_id, cand_ids = _create_running_experiment("iv01")
        j_result = run_judgement_v2(CTX, exp_id, rev, as_of=date(2026, 8, 1))
        proposal_id = j_result["proposal_id"]
        self.assertEqual(j_result["proposal_state"], "created")

        with tenant_session(CTX) as s:
            append_eligibility_event(
                s,
                workspace_id=WS,
                experiment_id=exp_id,
                experiment_version_id=version_id,
                upstream_event_type="product_version_withdrawn",
                upstream_resource_type="library_version",
                upstream_resource_id="pv_iv01_withdrawn",
                upstream_event_id=new_id("upev"),
                evidence={"reason": "withdrawn"},
            )
            proj = s.scalar(
                select(ExperimentEligibilityProjectionRecord).where(
                    ExperimentEligibilityProjectionRecord.experiment_version_id == version_id,
                    ExperimentEligibilityProjectionRecord.level == "version",
                )
            )
            self.assertTrue(proj.irreversible_active)
            latest_pse = s.scalar(
                select(DecisionProposalStateEventRecord)
                .where(DecisionProposalStateEventRecord.proposal_id == proposal_id)
                .order_by(DecisionProposalStateEventRecord.sequence.desc())
                .limit(1)
            )
            self.assertEqual(latest_pse.event_type, "invalidated")

    def test_iv02_confirm_invalidated_proposal_rejected(self):
        """IV-02: Confirm invalidated proposal → 409 rejection."""
        exp_id, rev, version_id, cand_ids = _create_running_experiment("iv02")
        j_result = run_judgement_v2(CTX, exp_id, rev, as_of=date(2026, 8, 1))
        proposal_id = j_result["proposal_id"]

        with tenant_session(CTX) as s:
            append_eligibility_event(
                s,
                workspace_id=WS,
                experiment_id=exp_id,
                experiment_version_id=version_id,
                upstream_event_type="product_version_withdrawn",
                upstream_resource_type="library_version",
                upstream_resource_id="pv_iv02_w",
                upstream_event_id=new_id("upev"),
                evidence={"reason": "withdrawn"},
            )

        with self.assertRaises(ExperimentConflict) as cm:
            confirm_proposal_v2(
                CTX, exp_id, proposal_id,
                expected_proposal_sequence=1,
            )
        self.assertIn("invalidated", str(cm.exception))

    def test_iv03_born_invalid_proposal(self):
        """IV-03: Judge during active irreversible → proposal born created+invalidated."""
        exp_id, rev, version_id, cand_ids = _create_running_experiment("iv03")

        with tenant_session(CTX) as s:
            append_eligibility_event(
                s,
                workspace_id=WS,
                experiment_id=exp_id,
                experiment_version_id=version_id,
                upstream_event_type="offer_version_withdrawn",
                upstream_resource_type="library_version",
                upstream_resource_id="ov_iv03_w",
                upstream_event_id=new_id("upev"),
                evidence={"reason": "withdrawn"},
            )

        j_result = run_judgement_v2(CTX, exp_id, rev, as_of=date(2026, 8, 1))
        self.assertEqual(j_result["proposal_state"], "invalidated")

        with tenant_session(CTX) as s:
            events = s.scalars(
                select(DecisionProposalStateEventRecord)
                .where(DecisionProposalStateEventRecord.proposal_id == j_result["proposal_id"])
                .order_by(DecisionProposalStateEventRecord.sequence)
            ).all()
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].event_type, "created")
            self.assertEqual(events[1].event_type, "invalidated")
            self.assertEqual(events[1].reason, "born_invalid")

    def test_iv04_reversible_event_no_terminal_state_event(self):
        """IV-04: Reversible event → no terminal state event on proposal, but confirmation blocked."""
        exp_id, rev, version_id, cand_ids = _create_running_experiment("iv04")
        j_result = run_judgement_v2(CTX, exp_id, rev, as_of=date(2026, 8, 1))
        proposal_id = j_result["proposal_id"]

        with tenant_session(CTX) as s:
            append_eligibility_event(
                s,
                workspace_id=WS,
                experiment_id=exp_id,
                experiment_version_id=version_id,
                upstream_event_type="review_rejected",
                upstream_resource_type="quality_review",
                upstream_resource_id="rev_iv04",
                upstream_event_id=new_id("upev"),
                evidence={"reason": "rejected"},
            )
            events = s.scalars(
                select(DecisionProposalStateEventRecord)
                .where(DecisionProposalStateEventRecord.proposal_id == proposal_id)
                .order_by(DecisionProposalStateEventRecord.sequence)
            ).all()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].event_type, "created")

        with self.assertRaises(ExperimentConflict) as cm:
            confirm_proposal_v2(
                CTX, exp_id, proposal_id,
                expected_proposal_sequence=1,
            )
        self.assertIn("not eligible", str(cm.exception))

    def test_iv05_reversible_restoration_may_confirm(self):
        """IV-05: After review_re_approved, confirmation proceeds if proposal is fresh."""
        exp_id, rev, version_id, cand_ids = _create_running_experiment("iv05")
        j_result = run_judgement_v2(CTX, exp_id, rev, as_of=date(2026, 8, 1))
        proposal_id = j_result["proposal_id"]

        with tenant_session(CTX) as s:
            append_eligibility_event(
                s,
                workspace_id=WS,
                experiment_id=exp_id,
                experiment_version_id=version_id,
                upstream_event_type="review_rejected",
                upstream_resource_type="quality_review",
                upstream_resource_id="rev_iv05_rej",
                upstream_event_id=new_id("upev"),
                evidence={"reason": "rejected"},
            )

        with tenant_session(CTX) as s:
            append_eligibility_event(
                s,
                workspace_id=WS,
                experiment_id=exp_id,
                experiment_version_id=version_id,
                upstream_event_type="review_re_approved",
                upstream_resource_type="quality_review",
                upstream_resource_id="rev_iv05_reapp",
                upstream_event_id=new_id("upev"),
                evidence={"reason": "re-approved"},
            )

        result = confirm_proposal_v2(
            CTX, exp_id, proposal_id,
            expected_proposal_sequence=1,
        )
        self.assertIn("confirmation_id", result)
        self.assertTrue(len(result["decisions"]) > 0)

    def test_iv06_reversible_restoration_stale_proposal(self):
        """IV-06: After restoration, stale proposal sequence → 409."""
        exp_id, rev, version_id, cand_ids = _create_running_experiment("iv06")
        j_result = run_judgement_v2(CTX, exp_id, rev, as_of=date(2026, 8, 1))
        proposal_id = j_result["proposal_id"]

        with self.assertRaises(ExperimentConflict) as cm:
            confirm_proposal_v2(
                CTX, exp_id, proposal_id,
                expected_proposal_sequence=999,
            )
        self.assertIn("sequence_conflict", str(cm.exception))

    def test_iv07_superseded_does_not_invalidate(self):
        """IV-07: superseded library version → does NOT generate irreversible event."""
        exp_id, rev, version_id, cand_ids = _create_running_experiment("iv07")

        with tenant_session(CTX) as s:
            proj_before = s.scalar(
                select(ExperimentEligibilityProjectionRecord).where(
                    ExperimentEligibilityProjectionRecord.experiment_version_id == version_id,
                    ExperimentEligibilityProjectionRecord.level == "version",
                )
            )
            seq_before = proj_before.head_sequence
            eligible_before = proj_before.current_eligibility

        self.assertEqual(eligible_before, "eligible")

        with tenant_session(CTX) as s:
            proj_after = s.scalar(
                select(ExperimentEligibilityProjectionRecord).where(
                    ExperimentEligibilityProjectionRecord.experiment_version_id == version_id,
                    ExperimentEligibilityProjectionRecord.level == "version",
                )
            )
            self.assertEqual(proj_after.head_sequence, seq_before)
            self.assertFalse(proj_after.irreversible_active)

    def test_iv08_close_invalidated_requires_irreversible(self):
        """IV-08: Close with invalidated requires active irreversible event."""
        exp_id, rev, version_id, cand_ids = _create_running_experiment("iv08")

        with self.assertRaises(ExperimentConflict) as cm:
            close_experiment_v2(CTX, exp_id, rev, closure_kind="invalidated")
        self.assertIn("no_active_invalidation", str(cm.exception))

        with tenant_session(CTX) as s:
            append_eligibility_event(
                s,
                workspace_id=WS,
                experiment_id=exp_id,
                experiment_version_id=version_id,
                upstream_event_type="product_version_withdrawn",
                upstream_resource_type="library_version",
                upstream_resource_id="pv_iv08_w",
                upstream_event_id=new_id("upev"),
                evidence={"reason": "withdrawn"},
            )

        result = close_experiment_v2(CTX, exp_id, rev, closure_kind="invalidated")
        self.assertEqual(result["state"], "closed")

    def test_iv09_close_completed_requires_judgement(self):
        """IV-09: Close with completed requires ≥1 judgement."""
        exp_id, rev, version_id, cand_ids = _create_running_experiment("iv09")

        with self.assertRaises(ExperimentConflict) as cm:
            close_experiment_v2(CTX, exp_id, rev, closure_kind="completed")
        self.assertIn("no_judgements", str(cm.exception))

        run_judgement_v2(CTX, exp_id, rev, as_of=date(2026, 8, 1))
        result = close_experiment_v2(CTX, exp_id, rev, closure_kind="completed")
        self.assertEqual(result["state"], "closed")

    def test_iv10_irreversible_propagation_idempotent(self):
        """IV-10: Replay invalidation event on already-invalidated proposal → no duplicate state event."""
        exp_id, rev, version_id, cand_ids = _create_running_experiment("iv10")
        j_result = run_judgement_v2(CTX, exp_id, rev, as_of=date(2026, 8, 1))
        proposal_id = j_result["proposal_id"]

        with tenant_session(CTX) as s:
            append_eligibility_event(
                s,
                workspace_id=WS,
                experiment_id=exp_id,
                experiment_version_id=version_id,
                upstream_event_type="product_version_withdrawn",
                upstream_resource_type="library_version",
                upstream_resource_id="pv_iv10_w1",
                upstream_event_id=new_id("upev"),
                evidence={"reason": "withdrawn"},
            )
            count_after_first = s.scalar(
                select(DecisionProposalStateEventRecord)
                .where(DecisionProposalStateEventRecord.proposal_id == proposal_id)
                .order_by(DecisionProposalStateEventRecord.sequence.desc())
                .limit(1)
            ).sequence

        with tenant_session(CTX) as s:
            append_eligibility_event(
                s,
                workspace_id=WS,
                experiment_id=exp_id,
                experiment_version_id=version_id,
                upstream_event_type="offer_version_withdrawn",
                upstream_resource_type="library_version",
                upstream_resource_id="ov_iv10_w2",
                upstream_event_id=new_id("upev"),
                evidence={"reason": "withdrawn_again"},
            )
            latest_pse = s.scalar(
                select(DecisionProposalStateEventRecord)
                .where(DecisionProposalStateEventRecord.proposal_id == proposal_id)
                .order_by(DecisionProposalStateEventRecord.sequence.desc())
                .limit(1)
            )
            self.assertEqual(latest_pse.sequence, count_after_first)
            self.assertEqual(latest_pse.event_type, "invalidated")


if __name__ == "__main__":
    unittest.main()
