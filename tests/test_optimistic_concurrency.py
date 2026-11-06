"""OC-01..OC-08: Optimistic concurrency tests (§5.4.6)."""
from __future__ import annotations

import unittest
from datetime import date
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
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    GenerationRunRecord,
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
    add_entry_v2,
    bind_candidate_v2,
    close_experiment_v2,
    confirm_proposal_v2,
    create_experiment_v2,
    freeze_experiment_v2,
    run_judgement_v2,
    start_experiment_v2,
    update_context_v2,
)
from app.platform.tenancy import TenantContext
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)

WS = "ws_oc_test"
CTX = TenantContext(
    workspace_id=WS,
    role="owner",
    initiator_id="user_oc",
    request_id="req_oc",
    trace_id="trace_oc",
)


def _draft_context(suffix: str) -> dict:
    return {
        "market": "US",
        "currency": "USD",
        "placement": "feed",
        "product_version_id": f"pv_oc_{suffix}",
        "offer_version_id": f"ov_oc_{suffix}",
        "budget": {"daily_amount": "100", "total_amount": "3000"},
        "schedule": {
            "start_at": "2026-09-01T00:00:00+00:00",
            "end_at": "2026-12-01T00:00:00+00:00",
            "timezone": "UTC",
        },
        "attribution": {"click_window_days": 7, "view_window_days": 1},
        "statistical_plan": {
            "primary_metric": "ctr",
            "alpha": "0.05",
            "bootstrap_samples": 2000,
            "ctr_threshold": "0.01",
        },
    }


def _seed_assets(suffix: str) -> list[str]:
    with tenant_session(CTX) as s:
        if s.get(WorkspaceRecord, WS) is None:
            s.add(WorkspaceRecord(id=WS, name="OC Test"))
            s.flush()
        for lib_id, etype in [
            (f"pv_oc_{suffix}", "product"),
            (f"ov_oc_{suffix}", "offer"),
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
        prod_id = f"prod_oc_{suffix}"
        if s.get(ProductRecord, prod_id) is None:
            s.add(ProductRecord(id=prod_id, workspace_id=WS, name=f"P {suffix}"))
            s.flush()
        govern_product_offer_versions(
            s, CTX, product_id=prod_id,
            product_version_id=f"pv_oc_{suffix}",
            offer_version_id=f"ov_oc_{suffix}",
        )
        plan_id = f"plan_oc_{suffix}"
        if s.get(PlanRecord, plan_id) is None:
            s.add(PlanRecord(
                id=plan_id, workspace_id=WS, product_id=prod_id,
                mode="full", cap=0, total_full=1, snapshot_hash="a" * 64,
            ))
            s.flush()
        run_id = f"run_oc_{suffix}"
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
            asset_id = f"asset_oc_{suffix}_{i}"
            ver_id = f"ver_oc_{suffix}_{i}"
            if s.get(AssetRecord, asset_id) is None:
                s.add(AssetRecord(
                    id=asset_id, workspace_id=WS, product_id=prod_id,
                    run_id=run_id, combo_id="C01", combo_code="A1",
                    tags_json={}, filename=f"{suffix}_{i}.png",
                    provider="mock", model="mock", prompt="test",
                    fingerprint=f"fp_oc_{suffix}_{i}", status="ok",
                ))
                s.flush()
            if s.get(AssetVersionRecord, ver_id) is None:
                s.add(AssetVersionRecord(
                    id=ver_id, workspace_id=WS, asset_id=asset_id,
                    kind="generated", aspect_ratio="1:1", language="en",
                    object_key=f"{suffix}_{i}.png",
                    content_sha256=f"oc{suffix}{i}" + "a" * (64 - len(f"oc{suffix}{i}")),
                    byte_size=100, mime_type="image/png",
                ))
                s.flush()
            version_ids.append(ver_id)
        govern_asset_versions(
            s, CTX, product_id=prod_id,
            asset_version_ids=version_ids, tag=f"oc_{suffix}",
        )
        acct_id = f"mact_oc_{suffix}"
        if s.get(MetaAccountRecord, acct_id) is None:
            s.add(MetaAccountRecord(
                id=acct_id, workspace_id=WS,
                ad_account_id=f"act_oc_{suffix}",
                currency="USD", timezone_name="UTC", api_version="v23.0",
            ))
            s.flush()
        sync_id = f"sync_oc_{suffix}"
        if s.scalar(select(MetaSyncRunRecord).where(MetaSyncRunRecord.id == sync_id)) is None:
            s.add(MetaSyncRunRecord(
                id=sync_id, workspace_id=WS, account_id=acct_id,
                state="completed", date_start=date(2026, 8, 1),
                date_stop=date(2026, 8, 1), contract_hash="b" * 64,
                contract_json={}, api_version="v23.0",
                account_currency="USD", account_timezone="UTC",
                idempotency_key=f"oc-sync-{suffix}",
            ))
            s.flush()
        for i, ver_id in enumerate(version_ids):
            metric_id = f"metric_oc_{suffix}_{i}"
            existing = s.scalar(select(AdDailyMetricRecord).where(AdDailyMetricRecord.id == metric_id))
            if existing is None:
                s.add(AdDailyMetricRecord(
                    id=metric_id, account_id=acct_id, sync_run_id=sync_id,
                    ad_id=f"ad_oc_{suffix}_{i}", ad_name="test",
                    metric_date=date(2026, 8, 1),
                    spend=Decimal("50"), impressions=10000, clicks=500,
                    purchases=Decimal("10"), purchase_value=Decimal("500"),
                    currency="USD", contract_hash="b" * 64, raw_json={},
                ))
                s.flush()
    return version_ids


class OptimisticConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def test_oc01_freeze_stale_revision(self):
        """OC-01: freeze_experiment_v2 with stale revision → 409 revision_conflict."""
        ver_ids = _seed_assets("oc01")
        result = create_experiment_v2(CTX, product_id=f"prod_oc_oc01", name="OC01 Exp", draft_context=_draft_context("oc01"))
        exp_id = result["id"]
        rev = result["revision"]
        for vid in ver_ids:
            r = add_entry_v2(CTX, exp_id, asset_version_id=vid, expected_revision=rev)
            rev = r["revision"]

        with self.assertRaises(ExperimentConflict) as cm:
            freeze_experiment_v2(CTX, exp_id, rev - 1)
        self.assertIn("revision_conflict", str(cm.exception))

    def test_oc02_context_update_stale_revision(self):
        """OC-02: update_context_v2 with stale revision → 409 revision_conflict."""
        _seed_assets("oc02")
        result = create_experiment_v2(CTX, product_id="prod_oc_oc02", name="OC02 Exp", draft_context=_draft_context("oc02"))
        exp_id = result["id"]

        with self.assertRaises(ExperimentConflict) as cm:
            update_context_v2(
                CTX, exp_id,
                draft_context=_draft_context("oc02"),
                expected_revision=999,
            )
        self.assertIn("revision_conflict", str(cm.exception))

    def test_oc03_confirm_stale_proposal_sequence(self):
        """OC-03: confirm_proposal_v2 with stale proposal sequence → 409 sequence_conflict."""
        ver_ids = _seed_assets("oc03")
        result = create_experiment_v2(CTX, product_id="prod_oc_oc03", name="OC03 Exp", draft_context=_draft_context("oc03"))
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
                ad_account_id=f"act_oc_oc03",
                ad_id=f"ad_oc_oc03_{i}",
                expected_revision=rev,
            )
        started = start_experiment_v2(CTX, exp_id, rev)
        rev = started["revision"]
        j_result = run_judgement_v2(CTX, exp_id, rev, as_of=date(2026, 8, 1))
        proposal_id = j_result["proposal_id"]

        with self.assertRaises(ExperimentConflict) as cm:
            confirm_proposal_v2(
                CTX, exp_id, proposal_id,
                expected_proposal_sequence=999,
            )
        self.assertIn("sequence_conflict", str(cm.exception))

    def test_oc04_add_entry_stale_revision(self):
        """OC-04: add_entry_v2 with stale revision → 409 revision_conflict."""
        ver_ids = _seed_assets("oc04")
        result = create_experiment_v2(CTX, product_id="prod_oc_oc04", name="OC04 Exp", draft_context=_draft_context("oc04"))
        exp_id = result["id"]

        with self.assertRaises(ExperimentConflict) as cm:
            add_entry_v2(
                CTX, exp_id,
                asset_version_id=ver_ids[0],
                expected_revision=999,
            )
        self.assertIn("revision_conflict", str(cm.exception))

    def test_oc05_bind_stale_revision(self):
        """OC-05: bind_candidate_v2 with stale revision → 409 revision_conflict."""
        ver_ids = _seed_assets("oc05")
        result = create_experiment_v2(CTX, product_id="prod_oc_oc05", name="OC05 Exp", draft_context=_draft_context("oc05"))
        exp_id = result["id"]
        rev = result["revision"]
        for vid in ver_ids:
            r = add_entry_v2(CTX, exp_id, asset_version_id=vid, expected_revision=rev)
            rev = r["revision"]
        frozen = freeze_experiment_v2(CTX, exp_id, rev)
        rev = frozen["revision"]
        cand_ids = [c["id"] for c in frozen["candidates"]]

        with self.assertRaises(ExperimentConflict) as cm:
            bind_candidate_v2(
                CTX, exp_id, cand_ids[0],
                ad_account_id="act_oc_oc05",
                ad_id="ad_oc_oc05_0",
                expected_revision=rev - 1,
            )
        self.assertIn("revision_conflict", str(cm.exception))

    def test_oc06_start_stale_revision(self):
        """OC-06: start_experiment_v2 with stale revision → 409 revision_conflict."""
        ver_ids = _seed_assets("oc06")
        result = create_experiment_v2(CTX, product_id="prod_oc_oc06", name="OC06 Exp", draft_context=_draft_context("oc06"))
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
                ad_account_id=f"act_oc_oc06",
                ad_id=f"ad_oc_oc06_{i}",
                expected_revision=rev,
            )

        with self.assertRaises(ExperimentConflict) as cm:
            start_experiment_v2(CTX, exp_id, rev - 1)
        self.assertIn("revision_conflict", str(cm.exception))

    def test_oc07_judge_stale_revision(self):
        """OC-07: run_judgement_v2 with stale revision → 409 revision_conflict."""
        ver_ids = _seed_assets("oc07")
        result = create_experiment_v2(CTX, product_id="prod_oc_oc07", name="OC07 Exp", draft_context=_draft_context("oc07"))
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
                ad_account_id=f"act_oc_oc07",
                ad_id=f"ad_oc_oc07_{i}",
                expected_revision=rev,
            )
        started = start_experiment_v2(CTX, exp_id, rev)
        rev = started["revision"]

        with self.assertRaises(ExperimentConflict) as cm:
            run_judgement_v2(CTX, exp_id, rev - 1, as_of=date(2026, 8, 1))
        self.assertIn("revision_conflict", str(cm.exception))

    def test_oc08_close_stale_revision(self):
        """OC-08: close_experiment_v2 with stale revision → 409 revision_conflict."""
        ver_ids = _seed_assets("oc08")
        result = create_experiment_v2(CTX, product_id="prod_oc_oc08", name="OC08 Exp", draft_context=_draft_context("oc08"))
        exp_id = result["id"]
        rev = result["revision"]
        for vid in ver_ids:
            r = add_entry_v2(CTX, exp_id, asset_version_id=vid, expected_revision=rev)
            rev = r["revision"]
        frozen = freeze_experiment_v2(CTX, exp_id, rev)
        rev = frozen["revision"]

        with self.assertRaises(ExperimentConflict) as cm:
            close_experiment_v2(
                CTX, exp_id, rev - 1,
                closure_kind="abandoned_before_start",
            )
        self.assertIn("revision_conflict", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
