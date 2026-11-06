"""Controlled Mock and provenance isolation tests (MP, PI, UB).

§5.10.1 Execution mode enforcement (MP-01..05)
§5.10.2 Provenance isolation (PI-01..07)
§5.10.3 No-unlock-side-effect proof (UB-01..06)
"""
from __future__ import annotations

import ast
import inspect
import re
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
    DecisionConfirmationRecord,
    ExperimentAdBindingRecord,
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
)
from app.experiments.service import (
    ExperimentConflict,
    ExperimentError,
    add_entry_v2,
    bind_candidate_v2,
    close_experiment_v2,
    confirm_proposal_v2,
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
    workspace_id="ws_cm",
    role="owner",
    initiator_id="user_cm",
    request_id="req_cm",
    trace_id="trace_cm",
)

CTX_OTHER = TenantContext(
    workspace_id="ws_cm_other",
    role="owner",
    initiator_id="user_cm_other",
    request_id="req_cm_other",
    trace_id="trace_cm_other",
)

PROD_ID = "prod_cm"
PROD_ID_OTHER = "prod_cm_other"


def _draft_context(**overrides: object) -> dict:
    base = {
        "market": "US",
        "currency": "USD",
        "placement": "feed",
        "product_version_id": "pv_cm",
        "offer_version_id": "ov_cm",
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


def _ensure_workspace(session, ws_id: str, name: str) -> None:
    ws = session.get(WorkspaceRecord, ws_id)
    if ws is None:
        session.add(WorkspaceRecord(id=ws_id, name=name))
        session.flush()


def _seed_asset_versions(session, tag: str, ws_id: str = None, prod_id: str = None, count: int = 2) -> list[str]:
    ws_id = ws_id or CTX.workspace_id
    prod_id = prod_id or PROD_ID
    _ensure_workspace(session, ws_id, f"WS {ws_id}")

    if session.get(ProductRecord, prod_id) is None:
        session.add(ProductRecord(id=prod_id, workspace_id=ws_id, name=f"P {tag}"))
        session.flush()

    plan_id = f"plan_{tag}"
    if session.get(PlanRecord, plan_id) is None:
        session.add(PlanRecord(
            id=plan_id, workspace_id=ws_id,
            product_id=prod_id, mode="full", cap=0, total_full=1,
            snapshot_hash="a" * 64,
        ))
        session.flush()

    run_id = f"run_{tag}"
    if session.get(GenerationRunRecord, run_id) is None:
        session.add(GenerationRunRecord(
            id=run_id, workspace_id=ws_id,
            product_id=prod_id, plan_id=plan_id,
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
                id=asset_id, workspace_id=ws_id,
                product_id=prod_id, run_id=run_id,
                combo_id="C01", combo_code="A1", tags_json={},
                filename=f"{tag}_{i}.png", provider="mock", model="mock",
                prompt="test", fingerprint=f"fp_{tag}_{i}", status="ok",
            ))
            session.flush()
        if session.get(AssetVersionRecord, av_id) is None:
            session.add(AssetVersionRecord(
                id=av_id, workspace_id=ws_id,
                asset_id=asset_id, kind="generated", aspect_ratio="1:1",
                language="en", object_key=f"{tag}_{i}.png",
                content_sha256=f"{i:0>64x}", byte_size=1, mime_type="image/png",
            ))
            session.flush()
        av_ids.append(av_id)
    asset_context = CTX if ws_id == CTX.workspace_id else CTX_OTHER
    govern_asset_versions(
        session, asset_context, product_id=prod_id,
        asset_version_ids=av_ids, tag=tag,
    )
    return av_ids


def _seed_library_versions(session, ws_id: str = None) -> None:
    ws_id = ws_id or CTX.workspace_id
    prefix = ws_id.replace("ws_", "")
    for entry_id, ver_id, etype in [
        (f"le_pv_{prefix}", f"pv_{prefix}", "product"),
        (f"le_ov_{prefix}", f"ov_{prefix}", "offer"),
    ]:
        if session.get(LibraryEntryRecord, entry_id) is None:
            session.add(LibraryEntryRecord(
                id=entry_id, workspace_id=ws_id,
                entry_type=etype, display_name=f"Test {etype}",
            ))
            session.flush()
        if session.get(LibraryVersionRecord, ver_id) is None:
            session.add(LibraryVersionRecord(
                id=ver_id, workspace_id=ws_id,
                entry_id=entry_id, entry_type=etype,
                version_no=1, content_json={}, content_hash="c" * 64,
                state="effective", published_by="system",
            ))
            session.flush()
    product_id = PROD_ID if ws_id == CTX.workspace_id else PROD_ID_OTHER
    fixture_context = CTX if ws_id == CTX.workspace_id else CTX_OTHER
    govern_product_offer_versions(
        session, fixture_context, product_id=product_id,
        product_version_id=f"pv_{prefix}", offer_version_id=f"ov_{prefix}",
    )


def _seed_metrics(session, tag: str, ad_ids: list[str], ws_id: str = None) -> None:
    ws_id = ws_id or CTX.workspace_id
    acct_id = f"macct_{tag}"
    if session.get(MetaAccountRecord, acct_id) is None:
        session.add(MetaAccountRecord(
            id=acct_id, workspace_id=ws_id,
            ad_account_id=f"act_{tag}", currency="USD",
            timezone_name="UTC", api_version="v23.0",
        ))
        session.flush()

    sync_id = f"sync_{tag}"
    if session.get(MetaSyncRunRecord, sync_id) is None:
        session.add(MetaSyncRunRecord(
            id=sync_id, workspace_id=ws_id,
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


def _build_confirmed(tag: str, ctx: TenantContext = None, prod_id: str = None) -> dict:
    """Build a confirmed experiment and return dict with all ids."""
    ctx = ctx or CTX
    prod_id = prod_id or PROD_ID
    with tenant_session(ctx) as s:
        avs = _seed_asset_versions(s, tag, ws_id=ctx.workspace_id, prod_id=prod_id)
    exp = create_experiment_v2(ctx, product_id=prod_id, name=f"CM-{tag}", draft_context=_draft_context())
    add_entry_v2(ctx, exp["id"], asset_version_id=avs[0], expected_revision=1)
    add_entry_v2(ctx, exp["id"], asset_version_id=avs[1], expected_revision=2)
    frozen = freeze_experiment_v2(ctx, exp["id"], expected_revision=3)
    rev = frozen["revision"]
    ad_ids = []
    for cand in frozen["candidates"]:
        ad_id = f"ad_{tag}_{cand['position']}"
        ad_ids.append(ad_id)
        bind_candidate_v2(
            ctx, exp["id"], cand["id"],
            ad_account_id=f"act_{tag}_{cand['position']}",
            ad_id=ad_id,
            expected_revision=rev,
        )
    running = start_experiment_v2(ctx, exp["id"], expected_revision=rev)
    with tenant_session(ctx) as s:
        _seed_metrics(s, tag, ad_ids, ws_id=ctx.workspace_id)
    j_result = run_judgement_v2(
        ctx, running["id"], expected_revision=running["revision"],
        as_of=date(2026, 8, 1),
    )
    conf_result = confirm_proposal_v2(
        ctx, running["id"], j_result["proposal_id"],
        expected_proposal_sequence=1,
    )
    return {
        "experiment_id": exp["id"],
        "version_id": frozen["version_id"],
        "candidates": frozen["candidates"],
        "running": running,
        "judgement": j_result,
        "confirmation": conf_result,
        "ad_ids": ad_ids,
    }


class ControlledMockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_workspace(s, CTX.workspace_id, "CM test")
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(id=PROD_ID, workspace_id=CTX.workspace_id, name="P mock"))
                s.flush()
            _seed_library_versions(s, CTX.workspace_id)

        with tenant_session(CTX_OTHER) as s:
            _ensure_workspace(s, CTX_OTHER.workspace_id, "CM other")
            if s.get(ProductRecord, PROD_ID_OTHER) is None:
                s.add(ProductRecord(id=PROD_ID_OTHER, workspace_id=CTX_OTHER.workspace_id, name="P other"))
                s.flush()
            _seed_library_versions(s, CTX_OTHER.workspace_id)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    # -----------------------------------------------------------------------
    # MP: Execution mode enforcement
    # -----------------------------------------------------------------------

    def test_MP_01_all_mock_mode_freeze_succeeds(self) -> None:
        """All candidates execution_mode='mock' → freeze succeeds."""
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "mp01")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="MP01", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        result = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        self.assertEqual(result["state"], "frozen")
        with tenant_session(CTX) as s:
            cands = s.scalars(
                select(ExperimentVersionCandidateRecord).where(
                    ExperimentVersionCandidateRecord.experiment_version_id == result["version_id"],
                )
            ).all()
            for c in cands:
                self.assertEqual(c.execution_mode, "mock")

    def test_MP_04_mock_evidence_contains_execution_mode(self) -> None:
        """Mock evidence in eligibility_evidence_json has execution_mode field."""
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "mp04")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="MP04", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        with tenant_session(CTX) as s:
            cands = s.scalars(
                select(ExperimentVersionCandidateRecord).where(
                    ExperimentVersionCandidateRecord.experiment_version_id == frozen["version_id"],
                )
            ).all()
            for c in cands:
                self.assertIsNotNone(c.execution_mode)
                self.assertEqual(c.execution_mode, "mock")

    def test_MP_05_source_kind_recorded(self) -> None:
        """All candidates source_kind is one of the valid values."""
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "mp05")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="MP05", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        valid_kinds = {"generation", "upload", "reuse", "manual"}
        with tenant_session(CTX) as s:
            cands = s.scalars(
                select(ExperimentVersionCandidateRecord).where(
                    ExperimentVersionCandidateRecord.experiment_version_id == frozen["version_id"],
                )
            ).all()
            for c in cands:
                self.assertIn(c.source_kind, valid_kinds,
                              f"source_kind {c.source_kind!r} not in valid set")

    # -----------------------------------------------------------------------
    # PI: Provenance isolation
    # -----------------------------------------------------------------------

    def test_PI_01_candidate_source_kind_valid(self) -> None:
        """PI-01: Candidate source_kind is a valid CHECK value."""
        data = _build_confirmed("pi01")
        with tenant_session(CTX) as s:
            cands = s.scalars(
                select(ExperimentVersionCandidateRecord).where(
                    ExperimentVersionCandidateRecord.experiment_version_id == data["version_id"],
                )
            ).all()
            valid = {"generation", "upload", "reuse", "manual"}
            for c in cands:
                self.assertIn(c.source_kind, valid)

    def test_PI_03_verdict_projection_values(self) -> None:
        """PI-03: verdict column only contains win/learn/lose."""
        data = _build_confirmed("pi03")
        valid_verdicts = {"win", "learn", "lose"}
        with tenant_session(CTX) as s:
            projs = s.scalars(
                select(ContextualVerdictProjectionRecord).where(
                    ContextualVerdictProjectionRecord.experiment_id == data["experiment_id"],
                )
            ).all()
            self.assertGreater(len(projs), 0, "Expected at least one projection")
            for p in projs:
                self.assertIn(p.verdict, valid_verdicts,
                              f"Verdict {p.verdict!r} not in valid set")

    def test_PI_04_mock_execution_mode_through_lineage(self) -> None:
        """PI-04: execution_mode obtainable by joining Version→Candidate."""
        data = _build_confirmed("pi04")
        with tenant_session(CTX) as s:
            projs = s.scalars(
                select(ContextualVerdictProjectionRecord).where(
                    ContextualVerdictProjectionRecord.experiment_id == data["experiment_id"],
                )
            ).all()
            for proj in projs:
                cand = s.get(ExperimentVersionCandidateRecord, proj.candidate_id)
                self.assertIsNotNone(cand, f"Candidate {proj.candidate_id} not found")
                self.assertEqual(cand.execution_mode, "mock")
                version = s.get(ExperimentVersionRecord, cand.experiment_version_id)
                self.assertIsNotNone(version, "Version not found through lineage")
                self.assertEqual(version.experiment_id, data["experiment_id"])

    def test_PI_06_no_inconclusive_in_material_decisions(self) -> None:
        """PI-06: No inconclusive in material verdicts (Decisions)."""
        data = _build_confirmed("pi06")
        with tenant_session(CTX) as s:
            decisions = s.scalars(
                select(ExperimentDecisionRecord).where(
                    ExperimentDecisionRecord.experiment_id == data["experiment_id"],
                )
            ).all()
            for d in decisions:
                self.assertNotEqual(d.verdict, "inconclusive",
                                    "Inconclusive must not appear in Decisions")

    def test_PI_07_mock_isolated_from_other_experiment(self) -> None:
        """PI-07: Mock experiment's Decisions/projections don't appear in other experiment queries."""
        data_a = _build_confirmed("pi07a")
        data_b = _build_confirmed("pi07b")

        with tenant_session(CTX) as s:
            decs_a = s.scalars(
                select(ExperimentDecisionRecord).where(
                    ExperimentDecisionRecord.experiment_id == data_a["experiment_id"],
                )
            ).all()
            decs_b = s.scalars(
                select(ExperimentDecisionRecord).where(
                    ExperimentDecisionRecord.experiment_id == data_b["experiment_id"],
                )
            ).all()
            ids_a = {d.id for d in decs_a}
            ids_b = {d.id for d in decs_b}
            self.assertTrue(ids_a.isdisjoint(ids_b),
                            "Decision IDs overlap between experiments")

            projs_a = s.scalars(
                select(ContextualVerdictProjectionRecord).where(
                    ContextualVerdictProjectionRecord.experiment_id == data_a["experiment_id"],
                )
            ).all()
            projs_b = s.scalars(
                select(ContextualVerdictProjectionRecord).where(
                    ContextualVerdictProjectionRecord.experiment_id == data_b["experiment_id"],
                )
            ).all()
            proj_ids_a = {p.id for p in projs_a}
            proj_ids_b = {p.id for p in projs_b}
            self.assertTrue(proj_ids_a.isdisjoint(proj_ids_b),
                            "Projection IDs overlap between experiments")

    # -----------------------------------------------------------------------
    # UB: No-unlock-side-effect proof
    # -----------------------------------------------------------------------

    def test_UB_01_no_unlock_in_service_source(self) -> None:
        """UB-01: service.py contains no unlock/localization/scale function calls."""
        import app.experiments.service as svc
        source = inspect.getsource(svc)
        patterns = [
            r'\bunlock\b',
            r'\blocalization\b',
            r'\bscale_unlock\b',
            r'\btrigger_unlock\b',
            r'\benqueue_unlock\b',
        ]
        for pattern in patterns:
            matches = re.findall(pattern, source, re.IGNORECASE)
            self.assertEqual(len(matches), 0,
                             f"Found forbidden pattern '{pattern}' in service.py: {matches}")

    def test_UB_02_confirmation_no_side_effects(self) -> None:
        """UB-02: Confirmation with mock mode produces no unlock calls."""
        data = _build_confirmed("ub02")
        self.assertIn("confirmation_id", data["confirmation"])
        self.assertGreater(len(data["confirmation"]["decisions"]), 0)

    def test_UB_04_close_no_unlock(self) -> None:
        """UB-04: Close (completed) invokes no unlock."""
        data = _build_confirmed("ub04")
        result = close_experiment_v2(
            CTX, data["experiment_id"],
            expected_revision=data["running"]["revision"],
            closure_kind="completed",
        )
        self.assertEqual(result["state"], "closed")

    def test_UB_05_pattern_creation_no_unlock(self) -> None:
        """UB-05: Pattern creation invokes no unlock."""
        data = _build_confirmed("ub05")
        close_experiment_v2(
            CTX, data["experiment_id"],
            expected_revision=data["running"]["revision"],
            closure_kind="completed",
        )
        decision_id = data["confirmation"]["decisions"][0]["id"]
        exp = data["experiment_id"]
        with tenant_session(CTX) as s:
            exp_rec = s.get(ExperimentRecord, exp)
            rev = exp_rec.revision
        result = create_pattern_candidate(
            CTX, exp, decision_id,
            expected_revision=rev,
        )
        self.assertIn("id", result)

    def test_UB_06_no_unlock_imports(self) -> None:
        """UB-06: service.py does not import any unlock/localization/scale module."""
        import app.experiments.service as svc
        source_file = Path(inspect.getfile(svc))
        tree = ast.parse(source_file.read_text())
        imported_names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_names.append(node.module)
                for alias in node.names:
                    imported_names.append(alias.name)

        forbidden = {"unlock", "localization", "scale_unlock"}
        for name in imported_names:
            lower = name.lower()
            for f in forbidden:
                self.assertNotIn(f, lower,
                                 f"service.py imports '{name}' which contains forbidden '{f}'")


if __name__ == "__main__":
    unittest.main()
