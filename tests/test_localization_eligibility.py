"""Localization eligibility module tests (Issue #24, spec §16.1).

Exercises ``list_eligible_winners()`` and ``validate_source_for_batch()``
through their public interfaces only.

Winner Decisions are built through the real Experiments v2 flow
(``create_experiment_v2`` .. ``confirm_proposal_v2``) so the
Decision/confirmation/candidate/AssetVersion FK chain is exactly what
production code creates -- the same pattern
``tests/test_pattern_candidate.py`` uses. Each test scenario then appends
one purpose-built ``ExperimentDecisionRecord`` that reuses the real
chain's identity (candidate, proposal, confirmation) but sets exactly the
one field under test (verdict, confirmation, review, eligibility event,
Library version state, or requested evidence tier), mirroring that same
file's PC-03 technique for constructing a controlled Decision.
"""
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
    ContextualVerdictProjectionRecord,
    ExperimentDecisionRecord,
    ExperimentVersionCandidateRecord,
    GenerationRunRecord,
    JudgementRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    MetaAccountRecord,
    MetaSyncRunRecord,
    PlanRecord,
    ProductRecord,
    QualityReviewRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.experiments.eligibility import append_eligibility_event
from app.experiments.service import (
    add_entry_v2,
    bind_candidate_v2,
    confirm_proposal_v2,
    create_experiment_v2,
    freeze_experiment_v2,
    run_judgement_v2,
    start_experiment_v2,
)
from app.localization.eligibility import (
    EligibleWinner,
    ValidationReport,
    list_eligible_winners,
    validate_source_for_batch,
)
from app.platform.tenancy import TenantContext
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)

CTX = TenantContext(
    workspace_id="ws_elig",
    role="owner",
    initiator_id="user_elig",
    request_id="req_elig",
    trace_id="trace_elig",
)

CTX_OTHER = TenantContext(
    workspace_id="ws_elig_other",
    role="owner",
    initiator_id="user_elig_other",
    request_id="req_elig_other",
    trace_id="trace_elig_other",
)

PROD_ID = "prod_elig"


def _draft_context(pv_id: str, ov_id: str, **overrides: object) -> dict:
    base = {
        "market": "US",
        "currency": "USD",
        "placement": "feed",
        "product_version_id": pv_id,
        "offer_version_id": ov_id,
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


def _ensure_workspace(session, ws_id: str) -> None:
    ws = session.get(WorkspaceRecord, ws_id)
    if ws is None:
        session.add(WorkspaceRecord(id=ws_id, name=f"Eligibility test {ws_id}"))
        session.flush()


def _seed_library_versions(session, tag: str) -> tuple[str, str]:
    pv_entry, ov_entry = f"le_pv_{tag}", f"le_ov_{tag}"
    pv_id, ov_id = f"pv_{tag}", f"ov_{tag}"
    for entry_id, ver_id, etype in [(pv_entry, pv_id, "product"), (ov_entry, ov_id, "offer")]:
        session.add(LibraryEntryRecord(
            id=entry_id, workspace_id=CTX.workspace_id,
            entry_type=etype, display_name=f"Test {etype} {tag}",
        ))
        session.flush()
        session.add(LibraryVersionRecord(
            id=ver_id, workspace_id=CTX.workspace_id,
            entry_id=entry_id, entry_type=etype,
            version_no=1, content_json={}, content_hash="c" * 64,
            state="effective", published_by="system",
        ))
        session.flush()
    return pv_id, ov_id


def _seed_asset_versions(session, tag: str, count: int = 2) -> list[str]:
    _ensure_workspace(session, CTX.workspace_id)
    if session.get(ProductRecord, PROD_ID) is None:
        session.add(ProductRecord(id=PROD_ID, workspace_id=CTX.workspace_id, name="P Elig"))
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


def _seed_metrics(session, tag: str, ad_ids: list[str]) -> None:
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


def _build_confirmed_winner(tag: str) -> dict:
    """Drive a real experiment to a confirmed material Decision.

    Returns the identity fields needed to construct a purpose-built
    Decision that reuses this real, FK-valid chain (candidate, proposal,
    confirmation, entry) while a test scenario controls exactly one axis.
    """
    with tenant_session(CTX) as s:
        pv_id, ov_id = _seed_library_versions(s, tag)
        avs = _seed_asset_versions(s, tag)
        govern_product_offer_versions(
            s, CTX, product_id=PROD_ID,
            product_version_id=pv_id, offer_version_id=ov_id,
        )

    exp = create_experiment_v2(
        CTX, product_id=PROD_ID, name=f"ELIG-{tag}",
        draft_context=_draft_context(pv_id, ov_id),
    )
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
            ad_id=ad_id, expected_revision=rev,
        )
    running = start_experiment_v2(CTX, exp["id"], expected_revision=rev)
    with tenant_session(CTX) as s:
        _seed_metrics(s, tag, ad_ids)
    j_result = run_judgement_v2(
        CTX, running["id"], expected_revision=running["revision"],
        as_of=date(2026, 8, 1),
    )
    conf = confirm_proposal_v2(
        CTX, running["id"], j_result["proposal_id"],
        expected_proposal_sequence=1,
    )
    real_decision_id = conf["decisions"][0]["id"]

    with tenant_session(CTX) as s:
        real_decision = s.get(ExperimentDecisionRecord, real_decision_id)
        candidate = s.get(
            ExperimentVersionCandidateRecord,
            real_decision.experiment_version_candidate_id,
        )
        return {
            "tag": tag,
            "experiment_id": real_decision.experiment_id,
            "experiment_version_id": real_decision.experiment_version_id,
            "experiment_entry_id": real_decision.experiment_entry_id,
            "candidate_id": real_decision.experiment_version_candidate_id,
            "asset_version_id": candidate.asset_version_id,
            "proposal_id": real_decision.proposal_id,
            "confirmation_id": real_decision.confirmation_id,
            "product_version_id": pv_id,
            "offer_version_id": ov_id,
            "execution_mode": candidate.execution_mode,
        }


def _make_decision(
    chain: dict,
    *,
    verdict: str,
    confirmation_id: str | None,
    suffix: str = "",
) -> str:
    """Append one purpose-built Decision reusing ``chain``'s real identity."""
    with tenant_session(CTX) as s:
        judgement_id = new_id("judge")
        s.add(JudgementRecord(
            id=judgement_id, workspace_id=CTX.workspace_id,
            experiment_id=chain["experiment_id"],
            experiment_version_id=chain["experiment_version_id"],
            algorithm_version=f"test_v0{suffix}", as_of=date(2026, 8, 1),
            snapshot_hash=new_id("snap"), input_snapshot_json={}, result_json={},
            record_schema_version=2,
        ))
        s.flush()
        decision_id = new_id("decision")
        s.add(ExperimentDecisionRecord(
            id=decision_id, workspace_id=CTX.workspace_id,
            experiment_id=chain["experiment_id"], judgement_id=judgement_id,
            experiment_entry_id=chain["experiment_entry_id"],
            experiment_version_id=chain["experiment_version_id"],
            experiment_version_candidate_id=chain["candidate_id"],
            proposal_id=chain["proposal_id"], confirmation_id=confirmation_id,
            verdict=verdict, metric="ctr",
            actor_id=f"user_{chain['tag']}", decision_schema_version=2,
        ))
        s.flush()
    return decision_id


def _seed_review(asset_version_id: str, review_value: str | None) -> None:
    """Attach a QualityReview to an AssetVersion, or none when ``None``."""
    if review_value is None:
        with tenant_session(CTX) as s:
            reviews = s.scalars(select(QualityReviewRecord).where(
                QualityReviewRecord.asset_version_id == asset_version_id,
            )).all()
            for review in reviews:
                s.delete(review)
        return
    with tenant_session(CTX) as s:
        av = s.get(AssetVersionRecord, asset_version_id)
        s.add(QualityReviewRecord(
            id=new_id("qr"), workspace_id=CTX.workspace_id,
            product_id=PROD_ID, asset_id=av.asset_id,
            asset_version_id=asset_version_id, review_value=review_value,
            source="human", actor_id="reviewer_1",
        ))
        s.flush()


class TestSourceWinnerEligibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_workspace(s, CTX.workspace_id)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(id=PROD_ID, workspace_id=CTX.workspace_id, name="P Elig"))
                s.flush()
        with tenant_session(CTX_OTHER) as s:
            _ensure_workspace(s, CTX_OTHER.workspace_id)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    # -- rejection cases (spec §16.1) -----------------------------------

    def test_reject_no_decision(self) -> None:
        chain = _build_confirmed_winner("t01")
        with tenant_session(CTX) as s:
            report = validate_source_for_batch(
                s, CTX, chain["asset_version_id"], "decision_does_not_exist",
                requested_evidence_tier="mock",
            )
        self.assertIsInstance(report, ValidationReport)
        self.assertFalse(report.valid)
        self.assertEqual(report.findings, ("no_decision",))

    def test_reject_inconclusive_decision(self) -> None:
        chain = _build_confirmed_winner("t02")
        _seed_review(chain["asset_version_id"], "approved")
        decision_id = _make_decision(
            chain, verdict="inconclusive", confirmation_id=chain["confirmation_id"],
        )
        with tenant_session(CTX) as s:
            report = validate_source_for_batch(
                s, CTX, chain["asset_version_id"], decision_id,
                requested_evidence_tier="mock",
            )
        self.assertFalse(report.valid)
        self.assertIn("inconclusive_decision", report.findings)

    def test_reject_non_winner_decision(self) -> None:
        chain = _build_confirmed_winner("t03")
        _seed_review(chain["asset_version_id"], "approved")
        decision_id = _make_decision(
            chain, verdict="lose", confirmation_id=chain["confirmation_id"],
        )
        with tenant_session(CTX) as s:
            report = validate_source_for_batch(
                s, CTX, chain["asset_version_id"], decision_id,
                requested_evidence_tier="mock",
            )
        self.assertFalse(report.valid)
        self.assertIn("non_winner_decision", report.findings)
        self.assertNotIn("inconclusive_decision", report.findings)

    def test_reject_missing_confirmation(self) -> None:
        chain = _build_confirmed_winner("t04")
        _seed_review(chain["asset_version_id"], "approved")
        decision_id = _make_decision(chain, verdict="win", confirmation_id=None)
        with tenant_session(CTX) as s:
            report = validate_source_for_batch(
                s, CTX, chain["asset_version_id"], decision_id,
                requested_evidence_tier="mock",
            )
        self.assertFalse(report.valid)
        self.assertIn("missing_confirmation", report.findings)
        self.assertNotIn("non_winner_decision", report.findings)

    def test_reject_no_approved_review(self) -> None:
        for label, review_value, expected in (
            ("no review at all", None, "no_approved_review"),
            ("rejected review", "rejected", "review_rejected"),
            ("needs-changes review", "needs_changes", "review_needs_changes"),
        ):
            with self.subTest(label):
                chain = _build_confirmed_winner(f"t05_{expected}")
                _seed_review(chain["asset_version_id"], review_value)
                decision_id = _make_decision(
                    chain, verdict="win", confirmation_id=chain["confirmation_id"],
                )
                with tenant_session(CTX) as s:
                    report = validate_source_for_batch(
                        s, CTX, chain["asset_version_id"], decision_id,
                        requested_evidence_tier="mock",
                    )
                self.assertFalse(report.valid)
                self.assertIn(expected, report.findings)

    def test_reject_blocking_qc_failure(self) -> None:
        chain = _build_confirmed_winner("t06")
        _seed_review(chain["asset_version_id"], "approved")
        decision_id = _make_decision(
            chain, verdict="win", confirmation_id=chain["confirmation_id"],
        )
        with tenant_session(CTX) as s:
            append_eligibility_event(
                s, workspace_id=CTX.workspace_id,
                experiment_id=chain["experiment_id"],
                experiment_version_id=chain["experiment_version_id"],
                candidate_id=chain["candidate_id"],
                upstream_event_type="qc_failed_blocking",
                upstream_resource_type="asset_qc_attempt",
                upstream_resource_id=chain["asset_version_id"],
                upstream_event_id=new_id("qc"),
                evidence={"reason": "blocking_check_failed"},
            )

        with tenant_session(CTX) as s:
            report = validate_source_for_batch(
                s, CTX, chain["asset_version_id"], decision_id,
                requested_evidence_tier="mock",
            )
        self.assertFalse(report.valid)
        self.assertIn("qc_failed_blocking", report.findings)

    def test_reject_withdrawn_product(self) -> None:
        chain = _build_confirmed_winner("t07")
        _seed_review(chain["asset_version_id"], "approved")
        decision_id = _make_decision(
            chain, verdict="win", confirmation_id=chain["confirmation_id"],
        )
        with tenant_session(CTX) as s:
            product_version = s.get(LibraryVersionRecord, chain["product_version_id"])
            product_version.state = "withdrawn"
            s.add(product_version)

        with tenant_session(CTX) as s:
            report = validate_source_for_batch(
                s, CTX, chain["asset_version_id"], decision_id,
                requested_evidence_tier="mock",
            )
        self.assertFalse(report.valid)
        self.assertIn("product_withdrawn", report.findings)

    def test_reject_incompatible_evidence_tier(self) -> None:
        chain = _build_confirmed_winner("t08")
        _seed_review(chain["asset_version_id"], "approved")
        decision_id = _make_decision(
            chain, verdict="win", confirmation_id=chain["confirmation_id"],
        )
        # The winner's execution_mode is "mock" today (app/experiments/service.py
        # freezes every candidate at "mock" until real-provider Launch lands),
        # so requesting production-tier evidence for it is unsatisfiable.
        self.assertEqual(chain["execution_mode"], "mock")
        with tenant_session(CTX) as s:
            report = validate_source_for_batch(
                s, CTX, chain["asset_version_id"], decision_id,
                requested_evidence_tier="production",
            )
        self.assertFalse(report.valid)
        self.assertIn("incompatible_evidence_tier", report.findings)

    # -- acceptance and discovery/validation split -----------------------

    def test_accept_valid_winner(self) -> None:
        chain = _build_confirmed_winner("t09")
        _seed_review(chain["asset_version_id"], "approved")
        decision_id = _make_decision(
            chain, verdict="win", confirmation_id=chain["confirmation_id"],
        )
        with tenant_session(CTX) as s:
            report = validate_source_for_batch(
                s, CTX, chain["asset_version_id"], decision_id,
                requested_evidence_tier="mock",
            )
        self.assertTrue(report.valid)
        self.assertEqual(report.findings, ())

    def test_stale_discovery_projection_still_rejects(self) -> None:
        chain = _build_confirmed_winner("t10")
        _seed_review(chain["asset_version_id"], "approved")
        decision_id = _make_decision(
            chain, verdict="win", confirmation_id=chain["confirmation_id"],
        )

        # The discovery projection still shows this candidate as a Winner --
        # e.g. it has not been rebuilt since a downstream Truth change.
        with tenant_session(CTX) as s:
            s.add(ContextualVerdictProjectionRecord(
                id=new_id("cvp"), workspace_id=CTX.workspace_id,
                experiment_id=chain["experiment_id"],
                experiment_version_id=chain["experiment_version_id"],
                candidate_id=chain["candidate_id"], decision_id=decision_id,
                confirmation_id=chain["confirmation_id"], verdict="win",
                metric="ctr", asset_version_id=chain["asset_version_id"],
                product_version_id=chain["product_version_id"],
                offer_version_id=chain["offer_version_id"],
                market="US", placement="feed",
                landing_page_evidence_hash=("stale" + chain["tag"]).ljust(64, "0")[:64],
                confirmed_at=utcnow(),
            ))
            s.flush()

        # But the exact candidate's Truth has since gone stale.
        with tenant_session(CTX) as s:
            append_eligibility_event(
                s, workspace_id=CTX.workspace_id,
                experiment_id=chain["experiment_id"],
                experiment_version_id=chain["experiment_version_id"],
                candidate_id=chain["candidate_id"],
                upstream_event_type="stale_truth_detected",
                upstream_resource_type="truth_package",
                upstream_resource_id=new_id("tp"),
                upstream_event_id=new_id("stale"),
                evidence={"reason": "dependency_changed"},
            )

        with tenant_session(CTX) as s:
            discovered = list_eligible_winners(s, CTX)
        self.assertTrue(
            any(
                isinstance(w, EligibleWinner) and w.decision_id == decision_id
                for w in discovered
            ),
            "discovery projection should still surface the stale Winner",
        )

        with tenant_session(CTX) as s:
            report = validate_source_for_batch(
                s, CTX, chain["asset_version_id"], decision_id,
                requested_evidence_tier="mock",
            )
        self.assertFalse(report.valid)
        self.assertIn("stale_truth_detected", report.findings)

    # -- tenancy: authoritative validation and discovery are workspace-scoped

    def test_cross_workspace_decision_is_invisible(self) -> None:
        chain = _build_confirmed_winner("t11")
        _seed_review(chain["asset_version_id"], "approved")
        decision_id = _make_decision(
            chain, verdict="win", confirmation_id=chain["confirmation_id"],
        )
        with tenant_session(CTX_OTHER) as s:
            report = validate_source_for_batch(
                s, CTX_OTHER, chain["asset_version_id"], decision_id,
                requested_evidence_tier="mock",
            )
        self.assertFalse(report.valid)
        self.assertEqual(report.findings, ("no_decision",))

    def test_list_eligible_winners_scoped_to_workspace(self) -> None:
        chain = _build_confirmed_winner("t12")
        _seed_review(chain["asset_version_id"], "approved")
        decision_id = _make_decision(
            chain, verdict="win", confirmation_id=chain["confirmation_id"],
        )
        with tenant_session(CTX) as s:
            s.add(ContextualVerdictProjectionRecord(
                id=new_id("cvp"), workspace_id=CTX.workspace_id,
                experiment_id=chain["experiment_id"],
                experiment_version_id=chain["experiment_version_id"],
                candidate_id=chain["candidate_id"], decision_id=decision_id,
                confirmation_id=chain["confirmation_id"], verdict="win",
                metric="ctr", asset_version_id=chain["asset_version_id"],
                product_version_id=chain["product_version_id"],
                offer_version_id=chain["offer_version_id"],
                market="US", placement="feed",
                landing_page_evidence_hash=("scoped" + chain["tag"]).ljust(64, "0")[:64],
                confirmed_at=utcnow(),
            ))
            s.flush()

        with tenant_session(CTX) as s:
            own = list_eligible_winners(s, CTX)
        with tenant_session(CTX_OTHER) as s:
            other = list_eligible_winners(s, CTX_OTHER)

        self.assertTrue(any(w.decision_id == decision_id for w in own))
        self.assertFalse(any(w.decision_id == decision_id for w in other))


if __name__ == "__main__":
    unittest.main()
