"""Issue #22 acceptance-criterion traceability tests (§5.2).

Maps the seven GitHub Issue #22 acceptance criteria (AC-1 .. AC-7) to
executable evidence.  Each criterion has a dedicated test class; each
test method carries its AC test-ID.

AC-2 (gate enforcement) and AC-3 (concept conflict) are partially
covered here; full gate tests live in test_freeze_gates.py.
"""
from __future__ import annotations

import inspect
import os
import re
import shutil
import textwrap
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

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
    ExperimentAdBindingRecord,
    ExperimentDecisionRecord,
    ExperimentEligibilityProjectionRecord,
    ExperimentEntryRecord,
    ExperimentNotificationOutboxRecord,
    ExperimentRecord,
    ExperimentStateEventRecord,
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
    rebuild_projection,
)
from app.experiments.judge import judge
from app.experiments.outbox import append_notification
from app.experiments.service import (
    ExperimentConflict,
    ExperimentError,
    add_entry_v2,
    bind_candidate_v2,
    close_experiment_v2,
    confirm_proposal_v2,
    create_experiment_v2,
    freeze_experiment_v2,
    remove_entry_v2,
    run_judgement_v2,
    start_experiment_v2,
    update_context_v2,
    update_entry_v2,
)
from app.experiments.timeline import append_event as append_timeline_event
from app.platform.canonical import _canonical_json, _sha256
from app.platform.tenancy import TenantContext
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)

CTX = TenantContext(
    workspace_id="ws_ac",
    role="owner",
    initiator_id="user_ac",
    request_id="req_ac",
    trace_id="trace_ac",
)

PROD_ID = "prod_ac"


def _draft_context(**overrides: object) -> dict:
    base = {
        "market": "US",
        "currency": "USD",
        "placement": "feed",
        "product_version_id": "pv_ac",
        "offer_version_id": "ov_ac",
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
        session.add(WorkspaceRecord(id=CTX.workspace_id, name="AC test"))
        session.flush()


def _seed_asset_versions(session, tag: str, count: int = 2) -> list[str]:
    _ensure_workspace(session)
    if session.get(ProductRecord, PROD_ID) is None:
        session.add(ProductRecord(
            id=PROD_ID, workspace_id=CTX.workspace_id, name="P acceptance",
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


def _seed_library_versions(session) -> None:
    for entry_id, ver_id, etype in [
        ("le_pv_ac", "pv_ac", "product"),
        ("le_ov_ac", "ov_ac", "offer"),
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
        product_version_id="pv_ac", offer_version_id="ov_ac",
        markets=("US", "GB"),
    )


def _build_running(tag: str) -> tuple[dict, list[str]]:
    with tenant_session(CTX) as s:
        avs = _seed_asset_versions(s, tag)
    exp = create_experiment_v2(CTX, product_id=PROD_ID, name=f"AC-{tag}", draft_context=_draft_context())
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


# ---------------------------------------------------------------------------
# AC-1: Draft edits allowed only before freeze; frozen inputs and hashes
#        immutable
# ---------------------------------------------------------------------------

class TestAC1_DraftAndFreezeImmutability(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_workspace(s)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(
                    id=PROD_ID, workspace_id=CTX.workspace_id, name="P acceptance",
                ))
                s.flush()
            _seed_library_versions(s)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def test_AC1_01_add_entry_in_draft(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "ac1_01")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="AC1-01", draft_context=_draft_context())
        result = add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        self.assertEqual(result["revision"], 2)

    def test_AC1_02_update_entry_in_draft(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "ac1_02")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="AC1-02", draft_context=_draft_context())
        entry = add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        result = update_entry_v2(
            CTX, exp["id"], entry["id"],
            marketing_vars={"headline": "new"}, expected_revision=2,
        )
        self.assertEqual(result["revision"], 3)

    def test_AC1_03_remove_entry_in_draft(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "ac1_03")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="AC1-03", draft_context=_draft_context())
        entry = add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        result = remove_entry_v2(CTX, exp["id"], entry["id"], expected_revision=2)
        self.assertEqual(result["revision"], 3)

    def test_AC1_04_update_context_in_draft(self) -> None:
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="AC1-04", draft_context=_draft_context())
        result = update_context_v2(
            CTX, exp["id"], draft_context=_draft_context(market="GB"),
            expected_revision=1,
        )
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["draft_context"]["market"], "GB")

    def _frozen_experiment(self, tag: str) -> dict:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, tag)
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name=f"AC1-{tag}", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        return freeze_experiment_v2(CTX, exp["id"], expected_revision=3)

    def test_AC1_05_add_entry_after_freeze(self) -> None:
        frozen = self._frozen_experiment("ac1_05")
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "ac1_05_extra", count=1)
        with self.assertRaises((ExperimentConflict, ExperimentError)):
            add_entry_v2(CTX, frozen["id"], asset_version_id=avs[0], expected_revision=frozen["revision"])

    def test_AC1_06_update_entry_after_freeze(self) -> None:
        frozen = self._frozen_experiment("ac1_06")
        with tenant_session(CTX) as s:
            entry = s.scalar(
                select(ExperimentEntryRecord).where(
                    ExperimentEntryRecord.experiment_id == frozen["id"],
                ).limit(1)
            )
            entry_id = entry.id
        with self.assertRaises((ExperimentConflict, ExperimentError)):
            update_entry_v2(
                CTX, frozen["id"], entry_id,
                marketing_vars={"x": "y"}, expected_revision=frozen["revision"],
            )

    def test_AC1_07_remove_entry_after_freeze(self) -> None:
        frozen = self._frozen_experiment("ac1_07")
        with tenant_session(CTX) as s:
            entry = s.scalar(
                select(ExperimentEntryRecord).where(
                    ExperimentEntryRecord.experiment_id == frozen["id"],
                ).limit(1)
            )
            entry_id = entry.id
        with self.assertRaises((ExperimentConflict, ExperimentError)):
            remove_entry_v2(CTX, frozen["id"], entry_id, expected_revision=frozen["revision"])

    def test_AC1_08_update_context_after_freeze(self) -> None:
        frozen = self._frozen_experiment("ac1_08")
        with self.assertRaises((ExperimentConflict, ExperimentError)):
            update_context_v2(
                CTX, frozen["id"], draft_context=_draft_context(market="DE"),
                expected_revision=frozen["revision"],
            )

    def test_AC1_09_frozen_snapshot_hash_immutable_service(self) -> None:
        frozen = self._frozen_experiment("ac1_09")
        original_hash = frozen["frozen_snapshot_hash"]
        with tenant_session(CTX) as s:
            version = s.scalar(
                select(ExperimentVersionRecord).where(
                    ExperimentVersionRecord.experiment_id == frozen["id"],
                )
            )
            self.assertIsNotNone(version)
            self.assertEqual(version.frozen_snapshot_hash, original_hash)
            self.assertIsNotNone(version.frozen_snapshot_json)

    def test_AC1_13_frozen_hashes_verified_at_insert(self) -> None:
        frozen = self._frozen_experiment("ac1_13")
        with tenant_session(CTX) as s:
            version = s.scalar(
                select(ExperimentVersionRecord).where(
                    ExperimentVersionRecord.experiment_id == frozen["id"],
                )
            )
            snapshot_bytes = _canonical_json(version.frozen_snapshot_json).encode("utf-8")
            recomputed = _sha256(snapshot_bytes)
            self.assertEqual(version.frozen_snapshot_hash, recomputed)

    def test_AC1_14_projections_match_snapshot(self) -> None:
        frozen = self._frozen_experiment("ac1_14")
        with tenant_session(CTX) as s:
            version = s.scalar(
                select(ExperimentVersionRecord).where(
                    ExperimentVersionRecord.experiment_id == frozen["id"],
                )
            )
            snap = version.frozen_snapshot_json
            self.assertEqual(version.candidates_hash, snap["candidates_hash"])
            self.assertEqual(version.concept_resolutions_hash, snap.get("resolutions_hash"))


# ---------------------------------------------------------------------------
# AC-4: Pure judge deterministic; DB/time/environment changes do not affect
#        identical evidence
# ---------------------------------------------------------------------------

def _make_snapshot(*, tag: str = "s1") -> dict:
    """Build a minimal valid judge snapshot for CTR metric."""
    entries = []
    for i in range(2):
        daily = [
            {
                "date": f"2026-07-{25 + d:02d}",
                "spend": "10.00",
                "impressions": 1000 * (i + 1),
                "clicks": 50 * (i + 1) + (10 if i == 0 else 0),
                "purchases": "2",
                "purchase_value": "40",
            }
            for d in range(7)
        ]
        entries.append({
            "id": f"cand_{tag}_{i}",
            "position": i,
            "ad_id": f"ad_{tag}_{i}",
            "data_quality_reasons": [],
            "daily": daily,
        })
    snap = {
        "schema_version": 2,
        "algorithm_version": "judge-v1",
        "experiment": {
            "id": f"exp_{tag}",
            "frozen_snapshot_hash": "f" * 64,
            "config": {
                "primary_metric": "ctr",
                "ctr_threshold": "0.01",
                "alpha": "0.05",
                "bootstrap_samples": 2000,
            },
        },
        "as_of": "2026-08-01",
        "entries": entries,
    }
    from app.platform.canonical import canonical_hash
    snap["snapshot_hash"] = canonical_hash(snap)
    return snap


class TestAC4_PureJudgeDeterminism(unittest.TestCase):

    def test_AC4_01_judge_v1_regression(self) -> None:
        snap = _make_snapshot(tag="ac4_01")
        result = judge(snap)
        self.assertEqual(result["algorithm_version"], "judge-v1")
        self.assertEqual(len(result["entries"]), 2)
        for entry in result["entries"]:
            self.assertIn(entry["verdict"], {"win", "learn", "lose", "inconclusive"})

    def test_AC4_02_same_result_twice(self) -> None:
        snap = _make_snapshot(tag="ac4_02")
        r1 = judge(snap)
        r2 = judge(snap)
        self.assertEqual(r1, r2)

    def test_AC4_03_different_wall_clock(self) -> None:
        import time
        snap = _make_snapshot(tag="ac4_03")
        r1 = judge(snap)
        time.sleep(0.01)
        r2 = judge(snap)
        self.assertEqual(r1, r2)

    def test_AC4_07_reversed_candidate_order(self) -> None:
        snap = _make_snapshot(tag="ac4_07")
        r1 = judge(snap)
        snap_rev = dict(snap)
        snap_rev["entries"] = list(reversed(snap["entries"]))
        r2 = judge(snap_rev)
        r1_by_id = {e["entry_id"]: e for e in r1["entries"]}
        r2_by_id = {e["entry_id"]: e for e in r2["entries"]}
        self.assertEqual(r1_by_id, r2_by_id)

    def test_AC4_08_repeated_calls_x10(self) -> None:
        snap = _make_snapshot(tag="ac4_08")
        baseline = judge(snap)
        for _ in range(9):
            self.assertEqual(judge(snap), baseline)

    def test_AC4_09_source_scan_no_db_param(self) -> None:
        sig = inspect.signature(judge)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["snapshot"])
        for name in params:
            self.assertNotIn("session", name.lower())
            self.assertNotIn("connection", name.lower())
            self.assertNotIn("engine", name.lower())

    def test_AC4_10_source_scan_no_clock(self) -> None:
        src = Path("app/experiments/judge.py").read_text()
        self.assertNotIn("datetime.now", src)
        self.assertNotIn("time.time(", src)
        self.assertNotIn("time.monotonic(", src)
        self.assertNotIn("datetime.utcnow", src)

    def test_AC4_11_source_scan_no_unseeded_random(self) -> None:
        src = Path("app/experiments/judge.py").read_text()
        self.assertNotIn("random.choice(", src)
        self.assertNotIn("random.random(", src)
        self.assertNotIn("random.randint(", src)
        self.assertNotIn("random.shuffle(", src)
        self.assertNotIn("secrets.", src)
        self.assertIn("random.Random(", src)

    def test_AC4_12_dependency_denial_db(self) -> None:
        snap = _make_snapshot(tag="ac4_12")
        with mock.patch("app.experiments.judge.json.loads", side_effect=RuntimeError("DB poisoned")):
            result = judge(snap)
        self.assertEqual(len(result["entries"]), 2)

    def test_AC4_13_dependency_denial_clock(self) -> None:
        snap = _make_snapshot(tag="ac4_13")
        src = Path("app/experiments/judge.py").read_text()
        self.assertNotIn("datetime.now", src)
        self.assertNotIn("datetime.utcnow", src)
        self.assertNotIn("time.time(", src)
        result = judge(snap)
        self.assertEqual(len(result["entries"]), 2)

    def test_AC4_14_dependency_denial_environ(self) -> None:
        snap = _make_snapshot(tag="ac4_14")
        poison = mock.MagicMock()
        poison.__getitem__ = mock.Mock(side_effect=RuntimeError("env poisoned"))
        poison.__contains__ = mock.Mock(side_effect=RuntimeError("env poisoned"))
        poison.get = mock.Mock(side_effect=RuntimeError("env poisoned"))
        with mock.patch.dict(os.environ, {}, clear=True):
            result = judge(snap)
        self.assertEqual(len(result["entries"]), 2)

    def test_AC4_15_dependency_denial_secrets(self) -> None:
        snap = _make_snapshot(tag="ac4_15")
        with mock.patch("secrets.token_bytes", side_effect=RuntimeError("secrets poisoned")):
            result = judge(snap)
        self.assertEqual(len(result["entries"]), 2)


# ---------------------------------------------------------------------------
# AC-5: Judge appends Judgement/Proposal only; permitted actor confirms
#        data-sufficient Proposal
# ---------------------------------------------------------------------------

class TestAC5_JudgeAndConfirmSeparation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_workspace(s)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(
                    id=PROD_ID, workspace_id=CTX.workspace_id, name="P acceptance",
                ))
                s.flush()
            _seed_library_versions(s)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def _judged_experiment(self, tag: str) -> tuple[dict, dict]:
        running, ad_ids = _build_running(tag)
        with tenant_session(CTX) as s:
            _seed_metrics(s, tag, ad_ids)
        j_result = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )
        return running, j_result

    def test_AC5_01_judge_creates_judgement_and_proposal(self) -> None:
        _, j_result = self._judged_experiment("ac5_01")
        self.assertIn("judgement", j_result)
        self.assertIsNotNone(j_result["proposal_id"])
        self.assertEqual(j_result.get("proposal_state"), "created")

    def test_AC5_02_judge_does_not_create_decision(self) -> None:
        running, _ = self._judged_experiment("ac5_02")
        with tenant_session(CTX) as s:
            decisions = s.scalars(
                select(ExperimentDecisionRecord).where(
                    ExperimentDecisionRecord.experiment_id == running["id"],
                )
            ).all()
            self.assertEqual(len(decisions), 0)

    def test_AC5_03_judge_does_not_create_confirmation(self) -> None:
        running, j_result = self._judged_experiment("ac5_03")
        with tenant_session(CTX) as s:
            confs = s.scalars(
                select(DecisionConfirmationRecord).where(
                    DecisionConfirmationRecord.proposal_id == j_result["proposal_id"],
                )
            ).all()
            self.assertEqual(len(confs), 0)

    def test_AC5_04_judge_does_not_create_verdict_projection(self) -> None:
        running, _ = self._judged_experiment("ac5_04")
        with tenant_session(CTX) as s:
            projs = s.scalars(
                select(ContextualVerdictProjectionRecord).where(
                    ContextualVerdictProjectionRecord.experiment_id == running["id"],
                )
            ).all()
            self.assertEqual(len(projs), 0)

    def test_AC5_05_confirm_creates_decisions_and_projections(self) -> None:
        running, j_result = self._judged_experiment("ac5_05")
        result = confirm_proposal_v2(
            CTX, running["id"], j_result["proposal_id"],
            expected_proposal_sequence=1,
        )
        self.assertIn("confirmation_id", result)
        self.assertGreater(len(result["decisions"]), 0)
        with tenant_session(CTX) as s:
            projs = s.scalars(
                select(ContextualVerdictProjectionRecord).where(
                    ContextualVerdictProjectionRecord.experiment_id == running["id"],
                )
            ).all()
            self.assertGreater(len(projs), 0)

    def test_AC5_09_all_inconclusive_blocks_confirmation(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "ac5_09")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="AC5-09", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        rev = frozen["revision"]
        for cand in frozen["candidates"]:
            bind_candidate_v2(
                CTX, exp["id"], cand["id"],
                ad_account_id=f"act_ac5_09_{cand['position']}",
                ad_id=f"ad_ac5_09_{cand['position']}",
                expected_revision=rev,
            )
        running = start_experiment_v2(CTX, exp["id"], expected_revision=rev)
        j_result = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )
        with tenant_session(CTX) as s:
            proposal = s.get(DecisionProposalRecord, j_result["proposal_id"])
            all_inconcl = all(
                e["verdict"] == "inconclusive" for e in proposal.proposed_entries_json
            )
        if all_inconcl:
            with self.assertRaises(ExperimentConflict) as cm:
                confirm_proposal_v2(
                    CTX, running["id"], j_result["proposal_id"],
                    expected_proposal_sequence=1,
                )
            self.assertIn("no_material_verdicts", str(cm.exception))
        else:
            result = confirm_proposal_v2(
                CTX, running["id"], j_result["proposal_id"],
                expected_proposal_sequence=1,
            )
            self.assertGreater(len(result["decisions"]), 0)

    def test_AC5_10_confirmation_persists_actor_id(self) -> None:
        running, j_result = self._judged_experiment("ac5_10")
        result = confirm_proposal_v2(
            CTX, running["id"], j_result["proposal_id"],
            expected_proposal_sequence=1,
        )
        with tenant_session(CTX) as s:
            conf = s.get(DecisionConfirmationRecord, result["confirmation_id"])
            self.assertEqual(conf.actor_id, CTX.initiator_id)
            self.assertIsNotNone(conf.confirmed_at)


# ---------------------------------------------------------------------------
# AC-6: Inconclusive never material verdict; cannot unlock Localization or
#        Scale
# ---------------------------------------------------------------------------

class TestAC6_InconclusiveExclusion(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_workspace(s)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(
                    id=PROD_ID, workspace_id=CTX.workspace_id, name="P acceptance",
                ))
                s.flush()
            _seed_library_versions(s)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def _confirmed_experiment(self, tag: str) -> tuple[dict, dict, dict]:
        running, ad_ids = _build_running(tag)
        with tenant_session(CTX) as s:
            _seed_metrics(s, tag, ad_ids)
        j_result = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )
        c_result = confirm_proposal_v2(
            CTX, running["id"], j_result["proposal_id"],
            expected_proposal_sequence=1,
        )
        return running, j_result, c_result

    def test_AC6_02_inconclusive_excluded_from_decisions(self) -> None:
        running, j_result, c_result = self._confirmed_experiment("ac6_02")
        for dec in c_result["decisions"]:
            self.assertIn(dec["verdict"], {"win", "learn", "lose"})
            self.assertNotEqual(dec["verdict"], "inconclusive")

    def test_AC6_03_inconclusive_retained_in_judgement(self) -> None:
        running, j_result, _ = self._confirmed_experiment("ac6_03")
        with tenant_session(CTX) as s:
            j = s.scalar(
                select(JudgementRecord).where(
                    JudgementRecord.experiment_id == running["id"],
                ).limit(1)
            )
            verdicts = [e["verdict"] for e in j.result_json["entries"]]
            all_verdict_types = set(verdicts)
            self.assertTrue(
                all_verdict_types.issubset({"win", "learn", "lose", "inconclusive"})
            )

    def test_AC6_04_inconclusive_retained_in_proposal(self) -> None:
        running, j_result, _ = self._confirmed_experiment("ac6_04")
        with tenant_session(CTX) as s:
            proposal = s.get(DecisionProposalRecord, j_result["proposal_id"])
            verdicts = [e["verdict"] for e in proposal.proposed_entries_json]
            all_verdict_types = set(verdicts)
            self.assertTrue(
                all_verdict_types.issubset({"win", "learn", "lose", "inconclusive"})
            )

    def test_AC6_05_no_inconclusive_in_verdict_projections(self) -> None:
        running, _, _ = self._confirmed_experiment("ac6_05")
        with tenant_session(CTX) as s:
            projs = s.scalars(
                select(ContextualVerdictProjectionRecord).where(
                    ContextualVerdictProjectionRecord.experiment_id == running["id"],
                )
            ).all()
            for proj in projs:
                self.assertIn(proj.verdict, {"win", "learn", "lose"})
                self.assertNotEqual(proj.verdict, "inconclusive")

    def test_AC6_08_no_localization_unlock_side_effect(self) -> None:
        src_dir = Path("app/experiments")
        unlock_patterns = ["localization", "scale_unlock", "unlock_localization", "unlock_scale"]
        for py_file in src_dir.glob("*.py"):
            src = py_file.read_text()
            for pattern in unlock_patterns:
                if pattern in src.lower() and "no unlock" not in src.lower():
                    if "def " + pattern in src or "import " + pattern in src:
                        self.fail(f"Found unlock reference '{pattern}' in {py_file}")

    def test_AC6_09_no_scale_unlock_side_effect(self) -> None:
        src_dir = Path("app/experiments")
        for py_file in src_dir.glob("*.py"):
            src = py_file.read_text()
            if "scale" in src.lower():
                self.assertNotIn("def scale_unlock", src)
                self.assertNotIn("from .scale import", src)


# ---------------------------------------------------------------------------
# AC-7: Replays never rewrite Judgements/Decisions or duplicate notifications
# ---------------------------------------------------------------------------

class TestAC7_ReplayIdempotency(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_workspace(s)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(
                    id=PROD_ID, workspace_id=CTX.workspace_id, name="P acceptance",
                ))
                s.flush()
            _seed_library_versions(s)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def test_AC7_04_identical_judge_idempotent(self) -> None:
        running, ad_ids = _build_running("ac7_04")
        with tenant_session(CTX) as s:
            _seed_metrics(s, "ac7_04", ad_ids)
        r1 = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )
        r2 = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )
        self.assertEqual(
            r1["judgement"]["snapshot_hash"],
            r2["judgement"]["snapshot_hash"],
        )
        with tenant_session(CTX) as s:
            judgements = s.scalars(
                select(JudgementRecord).where(
                    JudgementRecord.experiment_id == running["id"],
                )
            ).all()
            self.assertEqual(len(judgements), 1)

    def test_AC7_06_eligibility_event_redelivery(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "ac7_06")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="AC7-06", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        version_id = frozen["version_id"]

        ev_id = new_id("upev")
        with tenant_session(CTX) as s:
            e1 = append_eligibility_event(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp["id"],
                experiment_version_id=version_id,
                upstream_event_type="review_rejected",
                upstream_resource_type="library_version",
                upstream_resource_id="rv_1",
                upstream_event_id=ev_id,
                evidence={"reason": "test"},
            )
            proj_after_first = s.scalar(
                select(ExperimentEligibilityProjectionRecord).where(
                    ExperimentEligibilityProjectionRecord.experiment_id == exp["id"],
                    ExperimentEligibilityProjectionRecord.level == "version",
                )
            )
            hash_after_first = proj_after_first.head_hash
            seq_after_first = proj_after_first.head_sequence

        self.assertEqual(seq_after_first, 1)

    def test_AC7_07_projection_rebuild_idempotent(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "ac7_07")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="AC7-07", draft_context=_draft_context())
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
                upstream_event_type="review_rejected",
                upstream_resource_type="library_version",
                upstream_resource_id="rv_ac7_07",
                upstream_event_id=new_id("upev"),
                evidence={"reason": "test"},
            )

        with tenant_session(CTX) as s:
            proj_before = s.scalar(
                select(ExperimentEligibilityProjectionRecord).where(
                    ExperimentEligibilityProjectionRecord.experiment_id == exp["id"],
                    ExperimentEligibilityProjectionRecord.level == "version",
                )
            )
            hash_before = proj_before.head_hash

            rebuilt = rebuild_projection(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp["id"],
                experiment_version_id=version_id,
                level="version",
            )
            self.assertEqual(rebuilt.head_hash, hash_before)

    def test_AC7_08_replay_does_not_rewrite_decision(self) -> None:
        running, ad_ids = _build_running("ac7_08")
        with tenant_session(CTX) as s:
            _seed_metrics(s, "ac7_08", ad_ids)
        j_result = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )
        c_result = confirm_proposal_v2(
            CTX, running["id"], j_result["proposal_id"],
            expected_proposal_sequence=1,
        )
        original_decisions = c_result["decisions"]

        c_replay = confirm_proposal_v2(
            CTX, running["id"], j_result["proposal_id"],
            expected_proposal_sequence=1,
        )
        self.assertTrue(c_replay.get("idempotent_replay"))
        replay_ids = {d["id"] for d in c_replay["decisions"]}
        original_ids = {d["id"] for d in original_decisions}
        self.assertEqual(replay_ids, original_ids)

    def test_AC7_09_replay_does_not_rewrite_judgement(self) -> None:
        running, ad_ids = _build_running("ac7_09")
        with tenant_session(CTX) as s:
            _seed_metrics(s, "ac7_09", ad_ids)
        r1 = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )
        r2 = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )
        self.assertEqual(r1["judgement"]["id"], r2["judgement"]["id"])
        with tenant_session(CTX) as s:
            j = s.get(JudgementRecord, r1["judgement"]["id"])
            self.assertIsNotNone(j)

    def test_AC7_10_notification_deduplication(self) -> None:
        running, ad_ids = _build_running("ac7_10")
        with tenant_session(CTX) as s:
            _seed_metrics(s, "ac7_10", ad_ids)
        j_result = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )
        confirm_proposal_v2(
            CTX, running["id"], j_result["proposal_id"],
            expected_proposal_sequence=1,
        )

        with tenant_session(CTX) as s:
            notifications = s.scalars(
                select(ExperimentNotificationOutboxRecord).where(
                    ExperimentNotificationOutboxRecord.experiment_id == running["id"],
                    ExperimentNotificationOutboxRecord.notification_type == "proposal_confirmed",
                )
            ).all()
            self.assertEqual(len(notifications), 1)

        c_replay = confirm_proposal_v2(
            CTX, running["id"], j_result["proposal_id"],
            expected_proposal_sequence=1,
        )
        self.assertTrue(c_replay.get("idempotent_replay"))

        with tenant_session(CTX) as s:
            notifications_after = s.scalars(
                select(ExperimentNotificationOutboxRecord).where(
                    ExperimentNotificationOutboxRecord.experiment_id == running["id"],
                    ExperimentNotificationOutboxRecord.notification_type == "proposal_confirmed",
                )
            ).all()
            self.assertEqual(len(notifications_after), 1)


if __name__ == "__main__":
    unittest.main()
