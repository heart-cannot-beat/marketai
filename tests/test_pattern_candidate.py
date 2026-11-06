"""Pattern candidate creation tests (PC-01..PC-16, SQLite-compatible subset).

Exercises ``create_pattern_candidate`` from a completed experiment with
material Decisions.
"""
from __future__ import annotations

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
    ExperimentNotificationOutboxRecord,
    ExperimentPatternCandidateRecord,
    ExperimentRecord,
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
    create_experiment,
    create_experiment_v2,
    create_pattern_candidate,
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
    workspace_id="ws_pc",
    role="owner",
    initiator_id="user_pc",
    request_id="req_pc",
    trace_id="trace_pc",
)

CTX_OTHER = TenantContext(
    workspace_id="ws_pc_other",
    role="owner",
    initiator_id="user_pc_other",
    request_id="req_pc_other",
    trace_id="trace_pc_other",
)

PROD_ID = "prod_pc"


def _draft_context(**overrides: object) -> dict:
    base = {
        "market": "US",
        "currency": "USD",
        "placement": "feed",
        "product_version_id": "pv_pc",
        "offer_version_id": "ov_pc",
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
        session.add(WorkspaceRecord(id=ws_id, name=f"PC test {ws_id}"))
        session.flush()


def _seed_library_versions(session) -> None:
    for entry_id, ver_id, etype in [
        ("le_pv_pc", "pv_pc", "product"),
        ("le_ov_pc", "ov_pc", "offer"),
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


def _seed_asset_versions(session, tag: str, count: int = 2) -> list[str]:
    _ensure_workspace(session)
    if session.get(ProductRecord, PROD_ID) is None:
        session.add(ProductRecord(id=PROD_ID, workspace_id=CTX.workspace_id, name="P PC"))
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
    exp = create_experiment_v2(CTX, product_id=PROD_ID, name=f"PC-{tag}", draft_context=_draft_context())
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


class PatternCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_workspace(s)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(id=PROD_ID, workspace_id=CTX.workspace_id, name="P PC"))
                s.flush()
            _seed_library_versions(s)
            govern_product_offer_versions(
                s, CTX, product_id=PROD_ID,
                product_version_id="pv_pc", offer_version_id="ov_pc",
            )
        with tenant_session(CTX_OTHER) as s:
            _ensure_workspace(s, CTX_OTHER.workspace_id)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    # --- PC-01: create from material win Decision ---
    def test_PC_01_create_from_material_decision(self) -> None:
        closed, decisions = _build_closed_with_decisions("pc01")
        decision = decisions[0]
        result = create_pattern_candidate(
            CTX, closed["id"], decision["id"],
            expected_revision=closed["revision"],
        )
        self.assertTrue(result["id"].startswith("epc_"))
        self.assertEqual(result["experiment_id"], closed["id"])
        self.assertEqual(result["decision_id"], decision["id"])
        self.assertEqual(result["candidate_id"], decision["candidate_id"])
        self.assertIn(result["verdict"], ("win", "learn", "lose"))
        self.assertGreater(result["concept_count"], 0)
        self.assertIn(result["execution_mode"], ("byok", "sandbox", "mock"))
        self.assertIsNotNone(result["source_concepts_hash"])

    # --- PC-02: create from second material Decision ---
    def test_PC_02_second_material_decision(self) -> None:
        closed, decisions = _build_closed_with_decisions("pc02")
        if len(decisions) < 2:
            self.skipTest("Only one material decision available")
        result = create_pattern_candidate(
            CTX, closed["id"], decisions[1]["id"],
            expected_revision=closed["revision"],
        )
        self.assertEqual(result["decision_id"], decisions[1]["id"])

    # --- PC-03: inconclusive Decision → ExperimentError ---
    def test_PC_03_inconclusive_decision(self) -> None:
        closed, decisions = _build_closed_with_decisions("pc03")
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
                actor_id="user_pc",
                decision_schema_version=2,
            )
            s.add(fake_decision)
            s.flush()
            inc_id = fake_decision.id

        with self.assertRaises(ExperimentError) as cm:
            create_pattern_candidate(
                CTX, closed["id"], inc_id,
                expected_revision=closed["revision"],
            )
        self.assertIn("inconclusive", str(cm.exception))

    # --- PC-04: INSERT-or-load (idempotent) ---
    def test_PC_04_idempotent_insert_or_load(self) -> None:
        closed, decisions = _build_closed_with_decisions("pc04")
        r1 = create_pattern_candidate(
            CTX, closed["id"], decisions[0]["id"],
            expected_revision=closed["revision"],
        )
        r2 = create_pattern_candidate(
            CTX, closed["id"], decisions[0]["id"],
            expected_revision=closed["revision"],
        )
        self.assertEqual(r1["id"], r2["id"])

    # --- PC-05: same semantics as PC-04, confirms dedup key ---
    def test_PC_05_returns_existing_on_replay(self) -> None:
        closed, decisions = _build_closed_with_decisions("pc05")
        r1 = create_pattern_candidate(
            CTX, closed["id"], decisions[0]["id"],
            expected_revision=closed["revision"],
        )
        r2 = create_pattern_candidate(
            CTX, closed["id"], decisions[0]["id"],
            expected_revision=closed["revision"],
        )
        self.assertEqual(r1, r2)

    # --- PC-06: non-completed experiment → ExperimentConflict ---
    def test_PC_06_not_closed(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "pc06")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="PC06", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        rev = frozen["revision"]
        ad_ids = []
        for cand in frozen["candidates"]:
            ad_id = f"ad_pc06_{cand['position']}"
            ad_ids.append(ad_id)
            bind_candidate_v2(
                CTX, exp["id"], cand["id"],
                ad_account_id=f"act_pc06_{cand['position']}",
                ad_id=ad_id, expected_revision=rev,
            )
        running = start_experiment_v2(CTX, exp["id"], expected_revision=rev)
        with tenant_session(CTX) as s:
            _seed_metrics(s, "pc06", ad_ids)
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
            create_pattern_candidate(
                CTX, running["id"], decision_id,
                expected_revision=running["revision"],
            )
        self.assertIn("closed", str(cm.exception))

    # --- PC-09: cross-workspace → ExperimentNotFound ---
    def test_PC_09_cross_workspace(self) -> None:
        closed, decisions = _build_closed_with_decisions("pc09")
        with self.assertRaises(ExperimentNotFound):
            create_pattern_candidate(
                CTX_OTHER, closed["id"], decisions[0]["id"],
                expected_revision=closed["revision"],
            )

    # --- PC-10: execution_mode carried from candidate ---
    def test_PC_10_execution_mode_carried(self) -> None:
        closed, decisions = _build_closed_with_decisions("pc10")
        result = create_pattern_candidate(
            CTX, closed["id"], decisions[0]["id"],
            expected_revision=closed["revision"],
        )
        self.assertEqual(result["execution_mode"], "mock")

    # --- PC-13: legacy experiment → SchemaVersionMismatch ---
    def test_PC_13_legacy_experiment(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "pc13")
        legacy = create_experiment(
            CTX, product_id=PROD_ID, name="PC13-legacy",
            config_snapshot={
                "primary_metric": "ctr",
                "ctr_threshold": "0.01",
                "alpha": "0.05",
                "bootstrap_samples": 2000,
            },
            metric_contract_hash="d" * 64,
            entries=[
                {"asset_version_id": avs[0]},
                {"asset_version_id": avs[1]},
            ],
        )
        with self.assertRaises(SchemaVersionMismatch):
            create_pattern_candidate(
                CTX, legacy, "decision_fake",
                expected_revision=1,
            )

    # --- PC-14: timeline event emitted ---
    def test_PC_14_timeline_event_emitted(self) -> None:
        closed, decisions = _build_closed_with_decisions("pc14")
        create_pattern_candidate(
            CTX, closed["id"], decisions[0]["id"],
            expected_revision=closed["revision"],
        )
        with tenant_session(CTX) as s:
            tl_events = s.scalars(
                select(ExperimentTimelineEventRecord).where(
                    ExperimentTimelineEventRecord.experiment_id == closed["id"],
                    ExperimentTimelineEventRecord.event_type == "pattern_candidate_created",
                )
            ).all()
            self.assertGreater(len(tl_events), 0)

    # --- PC-15: no notification outbox row ---
    def test_PC_15_no_notification(self) -> None:
        closed, decisions = _build_closed_with_decisions("pc15")
        create_pattern_candidate(
            CTX, closed["id"], decisions[0]["id"],
            expected_revision=closed["revision"],
        )
        with tenant_session(CTX) as s:
            notifs = s.scalars(
                select(ExperimentNotificationOutboxRecord).where(
                    ExperimentNotificationOutboxRecord.experiment_id == closed["id"],
                    ExperimentNotificationOutboxRecord.notification_type == "pattern_candidate_created",
                )
            ).all()
            self.assertEqual(len(notifs), 0)

    # --- PC-16: source_concepts_hash matches persisted candidate ---
    def test_PC_16_source_concepts_hash(self) -> None:
        closed, decisions = _build_closed_with_decisions("pc16")
        result = create_pattern_candidate(
            CTX, closed["id"], decisions[0]["id"],
            expected_revision=closed["revision"],
        )
        with tenant_session(CTX) as s:
            pc_row = s.get(ExperimentPatternCandidateRecord, result["id"])
            self.assertIsNotNone(pc_row)
            self.assertEqual(pc_row.source_concepts_hash, result["source_concepts_hash"])
            self.assertIsNotNone(pc_row.pattern_evidence_hash)
            self.assertIsNotNone(pc_row.pattern_evidence_json)

    # --- PC-XX: decision not found ---
    def test_PC_xx_decision_not_found(self) -> None:
        closed, _ = _build_closed_with_decisions("pcxx")
        with self.assertRaises(ExperimentNotFound):
            create_pattern_candidate(
                CTX, closed["id"], "decision_garbage",
                expected_revision=closed["revision"],
            )

    # --- PC-YY: cross-experiment decision ---
    def test_PC_yy_cross_experiment_decision(self) -> None:
        closed_a, _ = _build_closed_with_decisions("pcya")
        _, decisions_b = _build_closed_with_decisions("pcyb")
        with self.assertRaises(ExperimentNotFound):
            create_pattern_candidate(
                CTX, closed_a["id"], decisions_b[0]["id"],
                expected_revision=closed_a["revision"],
            )

    # --- PC-ZZ: stale revision ---
    def test_PC_zz_stale_revision(self) -> None:
        closed, decisions = _build_closed_with_decisions("pczz")
        with self.assertRaises(ExperimentConflict) as cm:
            create_pattern_candidate(
                CTX, closed["id"], decisions[0]["id"],
                expected_revision=closed["revision"] - 1,
            )
        self.assertIn("revision", str(cm.exception).lower())


if __name__ == "__main__":
    unittest.main()
