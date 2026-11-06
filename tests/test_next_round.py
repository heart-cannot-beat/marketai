"""Next-round creation tests (NR-01..NR-22, SQLite-compatible subset).

Exercises ``create_next_round`` from a completed experiment with material
Decisions.
"""
from __future__ import annotations

import shutil
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from app.adapters.persistence.database import engine, tenant_session
from app.adapters.persistence.models import (
    AdDailyMetricRecord,
    AssetRecord,
    AssetVersionRecord,
    ExperimentDecisionRecord,
    ExperimentNextRoundRecord,
    ExperimentNotificationOutboxRecord,
    ExperimentTimelineEventRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    MetaAccountRecord,
    MetaSyncRunRecord,
    ProductRecord,
    WorkspaceRecord,
    new_id,
)
from app.experiments.service import (
    ExperimentConflict,
    ExperimentError,
    ExperimentNotFound,
    SchemaVersionMismatch,
    add_entry_v2,
    bind_candidate_v2,
    close_experiment_v2,
    confirm_proposal_v2,
    create_experiment_v2,
    create_next_round,
    freeze_experiment_v2,
    run_judgement_v2,
    start_experiment_v2,
)
from app.platform.tenancy import TenantContext
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)

CTX = TenantContext(
    workspace_id="ws_nr",
    role="owner",
    initiator_id="user_nr",
    request_id="req_nr",
    trace_id="trace_nr",
)

CTX_OTHER = TenantContext(
    workspace_id="ws_nr_other",
    role="owner",
    initiator_id="user_nr_other",
    request_id="req_nr_other",
    trace_id="trace_nr_other",
)

PROD_ID = "prod_nr"


def _draft_context(**overrides: object) -> dict:
    base = {
        "market": "US",
        "currency": "USD",
        "placement": "feed",
        "product_version_id": "pv_nr",
        "offer_version_id": "ov_nr",
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


def _ensure_workspace(session, ws_id: str = CTX.workspace_id) -> None:
    ws = session.get(WorkspaceRecord, ws_id)
    if ws is None:
        session.add(WorkspaceRecord(id=ws_id, name=f"NR test {ws_id}"))
        session.flush()


def _seed_library_versions(session) -> None:
    for entry_id, ver_id, etype in [
        ("le_pv_nr", "pv_nr", "product"),
        ("le_ov_nr", "ov_nr", "offer"),
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
        product_version_id="pv_nr", offer_version_id="ov_nr",
    )


def _seed_asset_versions(session, tag: str, count: int = 2) -> list[str]:
    _ensure_workspace(session)
    if session.get(ProductRecord, PROD_ID) is None:
        session.add(ProductRecord(id=PROD_ID, workspace_id=CTX.workspace_id, name="P NR"))
        session.flush()

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


def _build_closed_with_decisions(tag: str) -> tuple[dict, list[dict]]:
    """Drive experiment to closed state, return (experiment_dict, decisions)."""
    with tenant_session(CTX) as s:
        avs = _seed_asset_versions(s, tag)
    exp = create_experiment_v2(CTX, product_id=PROD_ID, name=f"NR-{tag}", draft_context=_draft_context())
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
    closed = close_experiment_v2(
        CTX, running["id"], expected_revision=running["revision"],
        closure_kind="completed",
    )
    return closed, conf["decisions"]


class NextRoundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_workspace(s)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(id=PROD_ID, workspace_id=CTX.workspace_id, name="P NR"))
                s.flush()
            _seed_library_versions(s)
        with tenant_session(CTX_OTHER) as s:
            _ensure_workspace(s, CTX_OTHER.workspace_id)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    # --- NR-01: valid next-round from completed experiment ---
    def test_NR_01_valid_next_round(self) -> None:
        closed, decisions = _build_closed_with_decisions("nr01")
        decision = decisions[0]
        result = create_next_round(
            CTX, closed["id"], decision["id"],
            intent_kind="same_recipe",
            expected_revision=closed["revision"],
        )
        self.assertTrue(result["id"].startswith("enrr_"))
        self.assertEqual(result["experiment_id"], closed["id"])
        self.assertEqual(result["decision_id"], decision["id"])
        self.assertEqual(result["intent_kind"], "same_recipe")
        self.assertIn("generation_draft_id", result)
        self.assertIn("source_candidate_id", result)
        self.assertIn("source_asset_version_id", result)

        with tenant_session(CTX) as s:
            tl = s.scalars(
                select(ExperimentTimelineEventRecord).where(
                    ExperimentTimelineEventRecord.experiment_id == closed["id"],
                    ExperimentTimelineEventRecord.event_type == "next_round_created",
                )
            ).all()
            self.assertGreater(len(tl), 0)

    # --- NR-02: valid from win Decision (same as NR-01 but explicit) ---
    def test_NR_02_valid_from_win_decision(self) -> None:
        closed, decisions = _build_closed_with_decisions("nr02")
        win_decision = next(
            (d for d in decisions if d["verdict"] in ("win", "learn", "lose")),
            decisions[0],
        )
        result = create_next_round(
            CTX, closed["id"], win_decision["id"],
            intent_kind="new_seed",
            expected_revision=closed["revision"],
        )
        self.assertEqual(result["intent_kind"], "new_seed")
        self.assertEqual(result["decision_id"], win_decision["id"])

    # --- NR-03: edit_prompt intent ---
    def test_NR_03_edit_prompt_intent(self) -> None:
        closed, decisions = _build_closed_with_decisions("nr03")
        result = create_next_round(
            CTX, closed["id"], decisions[0]["id"],
            intent_kind="edit_prompt",
            prompt_overrides={"headline": "new headline"},
            expected_revision=closed["revision"],
        )
        self.assertEqual(result["intent_kind"], "edit_prompt")

    # --- NR-04: non-completed experiment → ExperimentConflict ---
    def test_NR_04_not_closed(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "nr04")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="NR04", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        rev = frozen["revision"]
        ad_ids = []
        for cand in frozen["candidates"]:
            ad_id = f"ad_nr04_{cand['position']}"
            ad_ids.append(ad_id)
            bind_candidate_v2(
                CTX, exp["id"], cand["id"],
                ad_account_id=f"act_nr04_{cand['position']}",
                ad_id=ad_id, expected_revision=rev,
            )
        running = start_experiment_v2(CTX, exp["id"], expected_revision=rev)

        with tenant_session(CTX) as s:
            _seed_metrics(s, "nr04", ad_ids)
        j_result = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )
        conf = confirm_proposal_v2(
            CTX, running["id"], j_result["proposal_id"],
            expected_proposal_sequence=1,
        )
        decision_id = conf["decisions"][0]["id"]

        with self.assertRaises(ExperimentConflict) as cm:
            create_next_round(
                CTX, running["id"], decision_id,
                intent_kind="same_recipe",
                expected_revision=running["revision"],
            )
        self.assertIn("closed", str(cm.exception))

    # --- NR-05: inconclusive Decision → ExperimentError ---
    def test_NR_05_inconclusive_decision(self) -> None:
        closed, decisions = _build_closed_with_decisions("nr05")
        real = decisions[0]
        with tenant_session(CTX) as s:
            real_dec = s.get(ExperimentDecisionRecord, real["id"])
            fake_judgement_id = new_id("judge")
            from app.adapters.persistence.models import JudgementRecord as JR
            s.add(JR(
                id=fake_judgement_id,
                workspace_id=CTX.workspace_id,
                experiment_id=closed["id"],
                experiment_version_id=real_dec.experiment_version_id,
                algorithm_version="test_v0",
                as_of=date(2026, 8, 1),
                snapshot_hash=new_id("snap"),
                input_snapshot_json={},
                result_json={},
                record_schema_version=2,
            ))
            s.flush()
            fake_decision = ExperimentDecisionRecord(
                id=new_id("decision"),
                workspace_id=CTX.workspace_id,
                experiment_id=closed["id"],
                judgement_id=fake_judgement_id,
                experiment_entry_id=real_dec.experiment_entry_id,
                experiment_version_id=real_dec.experiment_version_id,
                experiment_version_candidate_id=real_dec.experiment_version_candidate_id,
                proposal_id=real_dec.proposal_id,
                confirmation_id=real_dec.confirmation_id,
                verdict="inconclusive",
                metric="ctr",
                actor_id="user_nr",
                decision_schema_version=2,
            )
            s.add(fake_decision)
            s.flush()
            inc_id = fake_decision.id

        with self.assertRaises(ExperimentError) as cm:
            create_next_round(
                CTX, closed["id"], inc_id,
                intent_kind="same_recipe",
                expected_revision=closed["revision"],
            )
        self.assertIn("inconclusive", str(cm.exception))

    # --- NR-06: Decision from different experiment → ExperimentNotFound ---
    def test_NR_06_cross_experiment_decision(self) -> None:
        closed_a, _ = _build_closed_with_decisions("nr06a")
        _, decisions_b = _build_closed_with_decisions("nr06b")
        with self.assertRaises(ExperimentNotFound):
            create_next_round(
                CTX, closed_a["id"], decisions_b[0]["id"],
                intent_kind="same_recipe",
                expected_revision=closed_a["revision"],
            )

    # --- NR-08: two calls same Decision → two records ---
    def test_NR_08_two_rounds_same_decision(self) -> None:
        closed, decisions = _build_closed_with_decisions("nr08")
        r1 = create_next_round(
            CTX, closed["id"], decisions[0]["id"],
            intent_kind="same_recipe",
            expected_revision=closed["revision"],
        )
        r2 = create_next_round(
            CTX, closed["id"], decisions[0]["id"],
            intent_kind="new_seed",
            expected_revision=closed["revision"],
        )
        self.assertNotEqual(r1["id"], r2["id"])
        self.assertNotEqual(r1["generation_draft_id"], r2["generation_draft_id"])

    # --- NR-09: stale revision → ExperimentConflict ---
    def test_NR_09_stale_revision(self) -> None:
        closed, decisions = _build_closed_with_decisions("nr09")
        with self.assertRaises(ExperimentConflict) as cm:
            create_next_round(
                CTX, closed["id"], decisions[0]["id"],
                intent_kind="same_recipe",
                expected_revision=closed["revision"] - 1,
            )
        self.assertIn("revision", str(cm.exception).lower())

    # --- NR-10: no auto-draft on judge ---
    def test_NR_10_no_auto_draft_on_judge(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "nr10")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="NR10", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        rev = frozen["revision"]
        ad_ids = []
        for cand in frozen["candidates"]:
            ad_id = f"ad_nr10_{cand['position']}"
            ad_ids.append(ad_id)
            bind_candidate_v2(
                CTX, exp["id"], cand["id"],
                ad_account_id=f"act_nr10_{cand['position']}",
                ad_id=ad_id, expected_revision=rev,
            )
        running = start_experiment_v2(CTX, exp["id"], expected_revision=rev)
        with tenant_session(CTX) as s:
            _seed_metrics(s, "nr10", ad_ids)
        run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )
        with tenant_session(CTX) as s:
            count = s.scalar(
                select(func.count()).select_from(ExperimentNextRoundRecord).where(
                    ExperimentNextRoundRecord.experiment_id == exp["id"],
                )
            )
            self.assertEqual(count, 0)

    # --- NR-11: no auto-draft on confirm ---
    def test_NR_11_no_auto_draft_on_confirm(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "nr11")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="NR11", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        rev = frozen["revision"]
        ad_ids = []
        for cand in frozen["candidates"]:
            ad_id = f"ad_nr11_{cand['position']}"
            ad_ids.append(ad_id)
            bind_candidate_v2(
                CTX, exp["id"], cand["id"],
                ad_account_id=f"act_nr11_{cand['position']}",
                ad_id=ad_id, expected_revision=rev,
            )
        running = start_experiment_v2(CTX, exp["id"], expected_revision=rev)
        with tenant_session(CTX) as s:
            _seed_metrics(s, "nr11", ad_ids)
        j_result = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )
        confirm_proposal_v2(
            CTX, running["id"], j_result["proposal_id"],
            expected_proposal_sequence=1,
        )
        with tenant_session(CTX) as s:
            count = s.scalar(
                select(func.count()).select_from(ExperimentNextRoundRecord).where(
                    ExperimentNextRoundRecord.experiment_id == exp["id"],
                )
            )
            self.assertEqual(count, 0)

    # --- NR-12: no auto-draft on close ---
    def test_NR_12_no_auto_draft_on_close(self) -> None:
        closed, _ = _build_closed_with_decisions("nr12")
        with tenant_session(CTX) as s:
            count = s.scalar(
                select(func.count()).select_from(ExperimentNextRoundRecord).where(
                    ExperimentNextRoundRecord.experiment_id == closed["id"],
                )
            )
            self.assertEqual(count, 0)

    # --- NR-14: cross-workspace → ExperimentNotFound ---
    def test_NR_14_cross_workspace(self) -> None:
        closed, decisions = _build_closed_with_decisions("nr14")
        with self.assertRaises(ExperimentNotFound):
            create_next_round(
                CTX_OTHER, closed["id"], decisions[0]["id"],
                intent_kind="same_recipe",
                expected_revision=closed["revision"],
            )

    # --- NR-18: quality_feedback requires qc_attempt_id ---
    def test_NR_18_quality_feedback_requires_qc_attempt(self) -> None:
        closed, decisions = _build_closed_with_decisions("nr18")
        with self.assertRaises(ExperimentError) as cm:
            create_next_round(
                CTX, closed["id"], decisions[0]["id"],
                intent_kind="quality_feedback",
                expected_revision=closed["revision"],
            )
        self.assertIn("qc_attempt_id", str(cm.exception))

    # --- NR-22: non-quality_feedback with qc_attempt_id → ExperimentError ---
    def test_NR_22_non_quality_feedback_with_qc_attempt(self) -> None:
        closed, decisions = _build_closed_with_decisions("nr22")
        with self.assertRaises(ExperimentError) as cm:
            create_next_round(
                CTX, closed["id"], decisions[0]["id"],
                intent_kind="same_recipe",
                qc_attempt_id="qc_fake_nr22",
                expected_revision=closed["revision"],
            )
        self.assertIn("forbidden", str(cm.exception))

    # --- NR-XX: invalid intent_kind ---
    def test_NR_xx_invalid_intent_kind(self) -> None:
        closed, decisions = _build_closed_with_decisions("nrxx")
        with self.assertRaises(ExperimentError) as cm:
            create_next_round(
                CTX, closed["id"], decisions[0]["id"],
                intent_kind="invalid_kind",
                expected_revision=closed["revision"],
            )
        self.assertIn("intent_kind", str(cm.exception))

    # --- NR-YY: notification outbox row created ---
    def test_NR_yy_outbox_notification_created(self) -> None:
        closed, decisions = _build_closed_with_decisions("nryy")
        create_next_round(
            CTX, closed["id"], decisions[0]["id"],
            intent_kind="same_recipe",
            expected_revision=closed["revision"],
        )
        with tenant_session(CTX) as s:
            notifs = s.scalars(
                select(ExperimentNotificationOutboxRecord).where(
                    ExperimentNotificationOutboxRecord.experiment_id == closed["id"],
                    ExperimentNotificationOutboxRecord.notification_type == "next_round_created",
                )
            ).all()
            self.assertGreater(len(notifs), 0)

    # --- NR-ZZ: decision not found (garbage id) ---
    def test_NR_zz_decision_not_found(self) -> None:
        closed, _ = _build_closed_with_decisions("nrzz")
        with self.assertRaises(ExperimentNotFound):
            create_next_round(
                CTX, closed["id"], "decision_garbage",
                intent_kind="same_recipe",
                expected_revision=closed["revision"],
            )


if __name__ == "__main__":
    unittest.main()
