"""DP-01..DP-16: Proposal, confirmation, and decision pipeline tests (§5.4.4)."""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.adapters.persistence.database import engine, platform_session, tenant_session
from app.adapters.persistence.models import (
    AdDailyMetricRecord,
    AssetRecord,
    AssetVersionRecord,
    ContextualVerdictProjectionRecord,
    DecisionConfirmationRecord,
    DecisionProposalRecord,
    DecisionProposalStateEventRecord,
    ExperimentDecisionRecord,
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
)
from app.experiments.service import (
    ExperimentConflict,
    ExperimentError,
    add_entry_v2,
    bind_candidate_v2,
    confirm_proposal_v2,
    create_experiment_v2,
    freeze_experiment_v2,
    run_judgement_v2,
    start_experiment_v2,
)
from app.platform.canonical import _canonical_json, _sha256
from app.platform.tenancy import TenantContext
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)

CTX = TenantContext(
    workspace_id="ws_dp",
    role="owner",
    initiator_id="user_dp",
    request_id="req_dp",
    trace_id="trace_dp",
)

def _draft_context(suffix: str) -> dict:
    return {
        "market": "US", "currency": "USD", "placement": "feed",
        "product_version_id": f"pv_dp_{suffix}",
        "offer_version_id": f"ov_dp_{suffix}",
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


def _seed_asset_versions(session, suffix: str, count: int = 2) -> list[str]:
    prod_id = f"prod_dp_{suffix}"
    plan_id = f"plan_dp_{suffix}"
    run_id = f"run_dp_{suffix}"

    for entry_type in ("product", "offer"):
        version_id = f"{'pv' if entry_type == 'product' else 'ov'}_dp_{suffix}"
        entry_id = f"le_{version_id}"
        session.add(LibraryEntryRecord(
            id=entry_id, workspace_id=CTX.workspace_id,
            entry_type=entry_type, display_name=f"{entry_type} {suffix}",
        ))
        session.flush()
        session.add(LibraryVersionRecord(
            id=version_id, workspace_id=CTX.workspace_id, entry_id=entry_id,
            entry_type=entry_type, version_no=1, content_json={},
            content_hash="a" * 64, published_by="test",
        ))
        session.flush()
    session.add(ProductRecord(
        id=prod_id, workspace_id=CTX.workspace_id, name=f"DpProd {suffix}",
    ))
    session.flush()
    govern_product_offer_versions(
        session, CTX, product_id=prod_id,
        product_version_id=f"pv_dp_{suffix}",
        offer_version_id=f"ov_dp_{suffix}",
    )
    session.add(PlanRecord(
        id=plan_id, workspace_id=CTX.workspace_id, product_id=prod_id,
        mode="full", cap=0, total_full=1, snapshot_hash="a" * 64,
    ))
    session.flush()
    session.add(GenerationRunRecord(
        id=run_id, workspace_id=CTX.workspace_id, product_id=prod_id,
        plan_id=plan_id, provider="mock", model="mock",
        aspect_ratio="1:1", image_size="1024", variants=1, use_ref=False,
        status="done", total=1, done=1, failed=0, est_cost=Decimal("0"),
        currency="USD",
    ))
    session.flush()

    av_ids = []
    for i in range(count):
        asset_id = f"asset_dp_{suffix}_{i}"
        av_id = f"av_dp_{suffix}_{i}"
        session.add(AssetRecord(
            id=asset_id, workspace_id=CTX.workspace_id, product_id=prod_id,
            run_id=run_id, combo_id="C01", combo_code="A1", tags_json={},
            filename=f"{suffix}_{i}.png", provider="mock", model="mock",
            prompt="test", fingerprint=f"fp-dp-{suffix}-{i}", status="ok",
        ))
        session.flush()
        session.add(AssetVersionRecord(
            id=av_id, workspace_id=CTX.workspace_id, asset_id=asset_id,
            kind="generated", aspect_ratio="1:1", language="en",
            object_key=f"{suffix}_{i}.png", content_sha256=f"{i:0>64}",
            byte_size=1, mime_type="image/png", currency="USD",
        ))
        av_ids.append(av_id)
    session.flush()
    govern_asset_versions(
        session, CTX, product_id=prod_id, asset_version_ids=av_ids,
        tag=f"dp_{suffix}",
    )
    return av_ids


def _seed_metrics(session, suffix: str, ad_ids: list[str]) -> None:
    acct_id = f"macct_dp_{suffix}"
    sync_id = f"msync_dp_{suffix}"
    session.add(MetaAccountRecord(
        id=acct_id, workspace_id=CTX.workspace_id,
        ad_account_id=f"act_dp_{suffix}", currency="USD",
        timezone_name="UTC", api_version="v23.0",
    ))
    session.flush()
    session.add(MetaSyncRunRecord(
        id=sync_id, workspace_id=CTX.workspace_id, account_id=acct_id,
        state="completed", date_start=date(2026, 7, 1), date_stop=date(2026, 8, 1),
        contract_hash="z" * 64, contract_json={}, api_version="v23.0",
        account_currency="USD", account_timezone="UTC",
        idempotency_key=f"dp-sync-{suffix}",
    ))
    session.flush()

    for ad_id in ad_ids:
        for day_offset in range(5):
            d = date(2026, 7, 20 + day_offset)
            session.add(AdDailyMetricRecord(
                id=new_id("metric"), account_id=acct_id, sync_run_id=sync_id,
                ad_id=ad_id, metric_date=d,
                spend=Decimal("10.00"), impressions=1000, clicks=50,
                purchases=Decimal("5"), purchase_value=Decimal("150"),
                currency="USD", contract_hash="z" * 64, raw_json={},
            ))
    session.flush()


def _create_running_experiment(suffix: str) -> dict:
    """Create, freeze, bind, start → return experiment + candidates info."""
    with tenant_session(CTX) as session:
        av_ids = _seed_asset_versions(session, suffix, count=2)

    result = create_experiment_v2(
        CTX, product_id=f"prod_dp_{suffix}", name=f"DP {suffix}",
        draft_context=_draft_context(suffix),
    )
    exp_id = result["id"]
    rev = result["revision"]

    for av_id in av_ids:
        entry = add_entry_v2(CTX, exp_id, asset_version_id=av_id, expected_revision=rev)
        rev = entry["revision"]

    frozen = freeze_experiment_v2(CTX, exp_id, rev)
    rev = frozen["revision"]
    candidates = frozen["candidates"]

    with tenant_session(CTX) as session:
        acct_id = f"macct_dp_{suffix}"
        sync_id = f"msync_dp_{suffix}"
        session.add(MetaAccountRecord(
            id=acct_id, workspace_id=CTX.workspace_id,
            ad_account_id=f"act_dp_{suffix}", currency="USD",
            timezone_name="UTC", api_version="v23.0",
        ))
        session.flush()
        session.add(MetaSyncRunRecord(
            id=sync_id, workspace_id=CTX.workspace_id, account_id=acct_id,
            state="completed", date_start=date(2026, 7, 1),
            date_stop=date(2026, 8, 1), contract_hash="z" * 64,
            contract_json={}, api_version="v23.0",
            account_currency="USD", account_timezone="UTC",
            idempotency_key=f"dp-sync-{suffix}",
        ))
        session.flush()

    ad_ids = []
    for i, cand in enumerate(candidates):
        ad_id = f"ad_dp_{suffix}_{i}"
        ad_ids.append(ad_id)
        bind_candidate_v2(
            CTX, exp_id, cand["id"],
            ad_account_id=f"act_dp_{suffix}", ad_id=ad_id,
            expected_revision=rev,
        )

    with tenant_session(CTX) as session:
        for ad_id in ad_ids:
            for day_offset in range(5):
                d = date(2026, 7, 20 + day_offset)
                session.add(AdDailyMetricRecord(
                    id=new_id("metric"), account_id=f"macct_dp_{suffix}",
                    sync_run_id=f"msync_dp_{suffix}",
                    ad_id=ad_id, metric_date=d,
                    spend=Decimal("10.00"), impressions=1000, clicks=50,
                    purchases=Decimal("5"), purchase_value=Decimal("150"),
                    currency="USD", contract_hash="z" * 64, raw_json={},
                ))
        session.flush()

    started = start_experiment_v2(CTX, exp_id, rev)
    return {
        "experiment_id": exp_id,
        "revision": started["revision"],
        "candidates": candidates,
        "ad_ids": ad_ids,
        "version_id": started.get("version_id"),
    }


class ProposalConfirmationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")

        with platform_session() as session:
            if session.get(WorkspaceRecord, CTX.workspace_id) is None:
                session.add(WorkspaceRecord(
                    id=CTX.workspace_id, name="DP Test WS",
                ))
                session.flush()

            for eid, vid, etype in [
                ("le_pv_dp", "pv_dp", "product"),
                ("le_ov_dp", "ov_dp", "offer"),
            ]:
                if session.get(LibraryEntryRecord, eid) is None:
                    session.add(LibraryEntryRecord(
                        id=eid, workspace_id=CTX.workspace_id,
                        entry_type=etype, display_name=f"{etype} dp",
                    ))
                    session.flush()
                if session.get(LibraryVersionRecord, vid) is None:
                    session.add(LibraryVersionRecord(
                        id=vid, workspace_id=CTX.workspace_id, entry_id=eid,
                        entry_type=etype, version_no=1, content_json={},
                        content_hash="a" * 64, published_by="test",
                    ))

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def test_dp01_create_proposal(self) -> None:
        info = _create_running_experiment("dp01")
        result = run_judgement_v2(
            CTX, info["experiment_id"], info["revision"],
            as_of=date(2026, 7, 24),
        )
        self.assertIn("proposal_id", result)
        self.assertIsNotNone(result["proposal_id"])

        with tenant_session(CTX) as session:
            proposal = session.get(DecisionProposalRecord, result["proposal_id"])
            self.assertIsNotNone(proposal)
            self.assertTrue(len(proposal.eligibility_context_hash) == 64)

            pse = session.scalar(
                select(DecisionProposalStateEventRecord).where(
                    DecisionProposalStateEventRecord.proposal_id == proposal.id,
                    DecisionProposalStateEventRecord.event_type == "created",
                )
            )
            self.assertIsNotNone(pse)
            self.assertEqual(pse.sequence, 1)

    def test_dp02_proposal_uniqueness_idempotent(self) -> None:
        info = _create_running_experiment("dp02")
        r1 = run_judgement_v2(
            CTX, info["experiment_id"], info["revision"],
            as_of=date(2026, 7, 24),
        )
        r2 = run_judgement_v2(
            CTX, info["experiment_id"], info["revision"],
            as_of=date(2026, 7, 24),
        )
        self.assertEqual(r1["proposal_id"], r2["proposal_id"])

    def test_dp03_confirm_creates_decisions_and_projections(self) -> None:
        info = _create_running_experiment("dp03")
        judge_result = run_judgement_v2(
            CTX, info["experiment_id"], info["revision"],
            as_of=date(2026, 7, 24),
        )
        proposal_id = judge_result["proposal_id"]

        conf_result = confirm_proposal_v2(
            CTX, info["experiment_id"], proposal_id,
            expected_proposal_sequence=1,
        )
        self.assertIn("confirmation_id", conf_result)
        self.assertIn("decisions", conf_result)
        self.assertGreater(len(conf_result["decisions"]), 0)

        with tenant_session(CTX) as session:
            confirmation = session.get(
                DecisionConfirmationRecord, conf_result["confirmation_id"]
            )
            self.assertIsNotNone(confirmation)
            self.assertEqual(confirmation.actor_id, CTX.initiator_id)

            decisions = session.scalars(
                select(ExperimentDecisionRecord).where(
                    ExperimentDecisionRecord.confirmation_id == confirmation.id,
                )
            ).all()
            self.assertGreater(len(decisions), 0)
            for d in decisions:
                self.assertEqual(d.actor_id, CTX.initiator_id)
                self.assertEqual(d.confirmed_at, confirmation.confirmed_at)
                self.assertIsNotNone(d.context_hash)
                self.assertEqual(len(d.context_hash), 64)

            projections = session.scalars(
                select(ContextualVerdictProjectionRecord).where(
                    ContextualVerdictProjectionRecord.experiment_id == info["experiment_id"],
                )
            ).all()
            self.assertGreater(len(projections), 0)

            consumed = session.scalar(
                select(DecisionProposalStateEventRecord).where(
                    DecisionProposalStateEventRecord.proposal_id == proposal_id,
                    DecisionProposalStateEventRecord.event_type == "consumed",
                )
            )
            self.assertIsNotNone(consumed)

    def test_dp04_confirmation_idempotent_replay(self) -> None:
        info = _create_running_experiment("dp04")
        judge_result = run_judgement_v2(
            CTX, info["experiment_id"], info["revision"],
            as_of=date(2026, 7, 24),
        )
        proposal_id = judge_result["proposal_id"]

        r1 = confirm_proposal_v2(
            CTX, info["experiment_id"], proposal_id,
            expected_proposal_sequence=1,
        )
        r2 = confirm_proposal_v2(
            CTX, info["experiment_id"], proposal_id,
            expected_proposal_sequence=1,
        )
        self.assertEqual(r1["confirmation_id"], r2["confirmation_id"])
        self.assertTrue(r2.get("idempotent_replay"))

    def test_dp07_confirm_during_irreversible_rejected(self) -> None:
        info = _create_running_experiment("dp07")
        judge_result = run_judgement_v2(
            CTX, info["experiment_id"], info["revision"],
            as_of=date(2026, 7, 24),
        )
        proposal_id = judge_result["proposal_id"]

        with tenant_session(CTX) as session:
            version = session.scalar(
                select(ExperimentVersionRecord).where(
                    ExperimentVersionRecord.experiment_id == info["experiment_id"],
                )
            )
            append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=info["experiment_id"],
                experiment_version_id=version.id,
                upstream_event_type="product_version_withdrawn",
                upstream_resource_type="library_version",
                upstream_resource_id="pv_dp07",
                upstream_event_id="uev_dp07",
                evidence={"reason": "withdrawn"},
            )

        with self.assertRaises(ExperimentConflict) as cm:
            confirm_proposal_v2(
                CTX, info["experiment_id"], proposal_id,
                expected_proposal_sequence=1,
            )
        self.assertIn("invalidated", str(cm.exception).lower())

    def test_dp08_all_inconclusive_blocks_confirmation(self) -> None:
        info = _create_running_experiment("dp08")
        judge_result = run_judgement_v2(
            CTX, info["experiment_id"], info["revision"],
            as_of=date(2026, 7, 24),
        )
        proposal_id = judge_result["proposal_id"]

        with tenant_session(CTX) as session:
            proposal = session.get(DecisionProposalRecord, proposal_id)
            all_inconclusive = [
                {**e, "verdict": "inconclusive"} for e in proposal.proposed_entries_json
            ]
            proposal.proposed_entries_json = all_inconclusive
            session.flush()

        with self.assertRaises(ExperimentConflict) as cm:
            confirm_proposal_v2(
                CTX, info["experiment_id"], proposal_id,
                expected_proposal_sequence=1,
            )
        self.assertIn("no_material_verdicts", str(cm.exception))

    def test_dp09_proposal_state_event_monotonic_sequence(self) -> None:
        info = _create_running_experiment("dp09")
        judge_result = run_judgement_v2(
            CTX, info["experiment_id"], info["revision"],
            as_of=date(2026, 7, 24),
        )
        proposal_id = judge_result["proposal_id"]

        confirm_proposal_v2(
            CTX, info["experiment_id"], proposal_id,
            expected_proposal_sequence=1,
        )

        with tenant_session(CTX) as session:
            events = session.scalars(
                select(DecisionProposalStateEventRecord)
                .where(DecisionProposalStateEventRecord.proposal_id == proposal_id)
                .order_by(DecisionProposalStateEventRecord.sequence)
            ).all()
            self.assertEqual(events[0].event_type, "created")
            self.assertEqual(events[0].sequence, 1)
            self.assertEqual(events[1].event_type, "consumed")
            self.assertEqual(events[1].sequence, 2)

    def test_dp12_decision_context_hash(self) -> None:
        info = _create_running_experiment("dp12")
        judge_result = run_judgement_v2(
            CTX, info["experiment_id"], info["revision"],
            as_of=date(2026, 7, 24),
        )
        conf = confirm_proposal_v2(
            CTX, info["experiment_id"], judge_result["proposal_id"],
            expected_proposal_sequence=1,
        )

        with tenant_session(CTX) as session:
            version = session.scalar(
                select(ExperimentVersionRecord).where(
                    ExperimentVersionRecord.experiment_id == info["experiment_id"],
                )
            )
            expected_hash = _sha256(
                _canonical_json({
                    "experiment_id": info["experiment_id"],
                    "offer_version_id": version.offer_version_id,
                    "market": version.market,
                    "placement": version.placement,
                    "landing_page_evidence_hash": version.landing_page_evidence_hash,
                }).encode("utf-8")
            )
            decisions = session.scalars(
                select(ExperimentDecisionRecord).where(
                    ExperimentDecisionRecord.confirmation_id == conf["confirmation_id"],
                )
            ).all()
            for d in decisions:
                self.assertEqual(d.context_hash, expected_hash)

    def test_dp15_born_invalid_proposal(self) -> None:
        info = _create_running_experiment("dp15")

        with tenant_session(CTX) as session:
            version = session.scalar(
                select(ExperimentVersionRecord).where(
                    ExperimentVersionRecord.experiment_id == info["experiment_id"],
                )
            )
            append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=info["experiment_id"],
                experiment_version_id=version.id,
                upstream_event_type="product_version_withdrawn",
                upstream_resource_type="library_version",
                upstream_resource_id="pv_dp15",
                upstream_event_id="uev_dp15",
                evidence={},
            )

        result = run_judgement_v2(
            CTX, info["experiment_id"], info["revision"],
            as_of=date(2026, 7, 24),
        )
        self.assertEqual(result["proposal_state"], "invalidated")

        with tenant_session(CTX) as session:
            events = session.scalars(
                select(DecisionProposalStateEventRecord)
                .where(
                    DecisionProposalStateEventRecord.proposal_id == result["proposal_id"],
                )
                .order_by(DecisionProposalStateEventRecord.sequence)
            ).all()
            types = [e.event_type for e in events]
            self.assertIn("created", types)
            self.assertIn("invalidated", types)

    def test_dp16_irreversible_invalidation_idempotent(self) -> None:
        info = _create_running_experiment("dp16")
        judge_result = run_judgement_v2(
            CTX, info["experiment_id"], info["revision"],
            as_of=date(2026, 7, 24),
        )
        proposal_id = judge_result["proposal_id"]

        with tenant_session(CTX) as session:
            version = session.scalar(
                select(ExperimentVersionRecord).where(
                    ExperimentVersionRecord.experiment_id == info["experiment_id"],
                )
            )
            append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=info["experiment_id"],
                experiment_version_id=version.id,
                upstream_event_type="product_version_withdrawn",
                upstream_resource_type="library_version",
                upstream_resource_id="pv_dp16a",
                upstream_event_id="uev_dp16a",
                evidence={},
            )

        with tenant_session(CTX) as session:
            version = session.scalar(
                select(ExperimentVersionRecord).where(
                    ExperimentVersionRecord.experiment_id == info["experiment_id"],
                )
            )
            append_eligibility_event(
                session, workspace_id=CTX.workspace_id,
                experiment_id=info["experiment_id"],
                experiment_version_id=version.id,
                upstream_event_type="offer_version_withdrawn",
                upstream_resource_type="library_version",
                upstream_resource_id="ov_dp16b",
                upstream_event_id="uev_dp16b",
                evidence={},
            )

        with tenant_session(CTX) as session:
            invalidated_events = session.scalars(
                select(DecisionProposalStateEventRecord).where(
                    DecisionProposalStateEventRecord.proposal_id == proposal_id,
                    DecisionProposalStateEventRecord.event_type == "invalidated",
                )
            ).all()
            self.assertEqual(len(invalidated_events), 1)


if __name__ == "__main__":
    unittest.main()
