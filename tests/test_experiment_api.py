"""Schema-2 experiment API tests (AS-01..AS-12 + error paths).

Tests the full HTTP interface for schema-2 experiment endpoints using
FastAPI TestClient with dependency-overridden TenantContext.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.adapters.persistence.database import engine, tenant_session
from app.adapters.persistence.models import (
    AdDailyMetricRecord,
    AssetRecord,
    AssetVersionRecord,
    ExperimentAdBindingRecord,
    ExperimentRecord,
    ExperimentVersionCandidateRecord,
    GenerationRunRecord,
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
from app.platform.tenancy import TenantContext
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)

CTX = TenantContext(
    workspace_id="ws_api",
    role="owner",
    initiator_id="user_api",
    request_id="req_api",
    trace_id="trace_api",
)

PROD_ID = "prod_api"


def _draft_context(**overrides: object) -> dict:
    base = {
        "market": "US",
        "currency": "USD",
        "placement": "feed",
        "product_version_id": "pv_api",
        "offer_version_id": "ov_api",
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
        session.add(WorkspaceRecord(id=CTX.workspace_id, name="API test"))
        session.flush()


def _seed_library_versions(session) -> None:
    for entry_id, ver_id, etype in [
        ("le_pv_api", "pv_api", "product"),
        ("le_ov_api", "ov_api", "offer"),
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
        session,
        CTX,
        product_id=PROD_ID,
        product_version_id="pv_api",
        offer_version_id="ov_api",
    )


def _seed_asset_versions(session, tag: str, count: int = 2) -> list[str]:
    _ensure_workspace(session)

    if session.get(ProductRecord, PROD_ID) is None:
        session.add(ProductRecord(
            id=PROD_ID, workspace_id=CTX.workspace_id, name="P api",
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
        session,
        CTX,
        product_id=PROD_ID,
        asset_version_ids=av_ids,
        tag=tag,
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


class ExperimentAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_workspace(s)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(
                    id=PROD_ID, workspace_id=CTX.workspace_id, name="P api",
                ))
                s.flush()
            _seed_library_versions(s)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.platform.http_identity import request_tenant_context
        app.dependency_overrides[request_tenant_context] = lambda: CTX
        return TestClient(app), app, request_tenant_context

    def _cleanup(self, app, dep):
        app.dependency_overrides.pop(dep, None)

    # --- AS-01: Create schema-2 experiment ---
    def test_AS_01_create_v2(self) -> None:
        client, app, dep = self._client()
        try:
            with client:
                resp = client.post("/api/experiments/v2", json={
                    "product_id": PROD_ID,
                    "name": "API Create Test",
                    "draft_context": _draft_context(),
                })
                self.assertEqual(resp.status_code, 201, resp.text)
                data = resp.json()
                self.assertEqual(data["record_schema_version"], 2)
                self.assertEqual(data["revision"], 1)
                self.assertEqual(data["state"], "draft")
                self.assertTrue(data["id"].startswith("exp_"))
        finally:
            self._cleanup(app, dep)

    # --- AS-02: GET schema-2 experiment ---
    def test_AS_02_get_v2(self) -> None:
        client, app, dep = self._client()
        try:
            with client:
                create_resp = client.post("/api/experiments/v2", json={
                    "product_id": PROD_ID,
                    "name": "API Get Test",
                    "draft_context": _draft_context(),
                })
                exp_id = create_resp.json()["id"]
                resp = client.get(f"/api/experiments/{exp_id}")
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertEqual(data["record_schema_version"], 2)
                self.assertEqual(data["id"], exp_id)
                self.assertIn("draft_context", data)
                self.assertIn("entries", data)
        finally:
            self._cleanup(app, dep)

    # --- AS-03: Add entry ---
    def test_AS_03_add_entry(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "api03")
        client, app, dep = self._client()
        try:
            with client:
                create_resp = client.post("/api/experiments/v2", json={
                    "product_id": PROD_ID,
                    "name": "API Add Entry",
                    "draft_context": _draft_context(),
                })
                exp_id = create_resp.json()["id"]
                resp = client.post(f"/api/experiments/{exp_id}/entries", json={
                    "asset_version_id": avs[0],
                    "expected_revision": 1,
                })
                self.assertEqual(resp.status_code, 201, resp.text)
                data = resp.json()
                self.assertEqual(data["revision"], 2)
                self.assertEqual(data["asset_version_id"], avs[0])
        finally:
            self._cleanup(app, dep)

    # --- AS-04: Update entry ---
    def test_AS_04_update_entry(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "api04")
        client, app, dep = self._client()
        try:
            with client:
                create_resp = client.post("/api/experiments/v2", json={
                    "product_id": PROD_ID,
                    "name": "API Update Entry",
                    "draft_context": _draft_context(),
                })
                exp_id = create_resp.json()["id"]
                add_resp = client.post(f"/api/experiments/{exp_id}/entries", json={
                    "asset_version_id": avs[0],
                    "expected_revision": 1,
                })
                entry_id = add_resp.json()["id"]
                resp = client.put(f"/api/experiments/{exp_id}/entries/{entry_id}", json={
                    "marketing_vars": {"headline": "updated"},
                    "expected_revision": 2,
                })
                self.assertEqual(resp.status_code, 200, resp.text)
                data = resp.json()
                self.assertEqual(data["revision"], 3)
                self.assertEqual(data["marketing_vars"], {"headline": "updated"})
        finally:
            self._cleanup(app, dep)

    # --- AS-05: Remove entry ---
    def test_AS_05_remove_entry(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "api05", count=3)
        client, app, dep = self._client()
        try:
            with client:
                create_resp = client.post("/api/experiments/v2", json={
                    "product_id": PROD_ID,
                    "name": "API Remove Entry",
                    "draft_context": _draft_context(),
                })
                exp_id = create_resp.json()["id"]
                rev = 1
                entry_ids = []
                for av in avs:
                    add_resp = client.post(f"/api/experiments/{exp_id}/entries", json={
                        "asset_version_id": av,
                        "expected_revision": rev,
                    })
                    entry_ids.append(add_resp.json()["id"])
                    rev += 1
                resp = client.request("DELETE", f"/api/experiments/{exp_id}/entries/{entry_ids[2]}", json={
                    "expected_revision": rev,
                })
                self.assertEqual(resp.status_code, 200, resp.text)
                data = resp.json()
                self.assertEqual(data["removed"], entry_ids[2])
        finally:
            self._cleanup(app, dep)

    # --- AS-06: Update context ---
    def test_AS_06_update_context(self) -> None:
        client, app, dep = self._client()
        try:
            with client:
                create_resp = client.post("/api/experiments/v2", json={
                    "product_id": PROD_ID,
                    "name": "API Update Context",
                    "draft_context": _draft_context(),
                })
                exp_id = create_resp.json()["id"]
                new_ctx = _draft_context(market="GB", currency="GBP")
                resp = client.put(f"/api/experiments/{exp_id}/context", json={
                    "draft_context": new_ctx,
                    "expected_revision": 1,
                })
                self.assertEqual(resp.status_code, 200, resp.text)
                data = resp.json()
                self.assertEqual(data["revision"], 2)
                self.assertEqual(data["draft_context"]["market"], "GB")
        finally:
            self._cleanup(app, dep)

    # --- AS-07: Freeze ---
    def test_AS_07_freeze(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "api07")
        client, app, dep = self._client()
        try:
            with client:
                create_resp = client.post("/api/experiments/v2", json={
                    "product_id": PROD_ID,
                    "name": "API Freeze",
                    "draft_context": _draft_context(),
                })
                exp_id = create_resp.json()["id"]
                rev = 1
                for av in avs:
                    client.post(f"/api/experiments/{exp_id}/entries", json={
                        "asset_version_id": av,
                        "expected_revision": rev,
                    })
                    rev += 1
                resp = client.post(f"/api/experiments/{exp_id}/freeze/v2", json={
                    "expected_revision": rev,
                })
                self.assertEqual(resp.status_code, 200, resp.text)
                data = resp.json()
                self.assertEqual(data["state"], "frozen")
                self.assertIsNotNone(data.get("frozen_snapshot_hash"))
                self.assertIn("candidates", data)
                self.assertEqual(len(data["candidates"]), 2)
        finally:
            self._cleanup(app, dep)

    # --- AS-08: Bind candidate ---
    def test_AS_08_bind_candidate(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "api08")
        client, app, dep = self._client()
        try:
            with client:
                create_resp = client.post("/api/experiments/v2", json={
                    "product_id": PROD_ID,
                    "name": "API Bind",
                    "draft_context": _draft_context(),
                })
                exp_id = create_resp.json()["id"]
                rev = 1
                for av in avs:
                    client.post(f"/api/experiments/{exp_id}/entries", json={
                        "asset_version_id": av,
                        "expected_revision": rev,
                    })
                    rev += 1
                freeze_resp = client.post(f"/api/experiments/{exp_id}/freeze/v2", json={
                    "expected_revision": rev,
                })
                cand_id = freeze_resp.json()["candidates"][0]["id"]
                freeze_rev = freeze_resp.json()["revision"]
                resp = client.post(f"/api/experiments/{exp_id}/candidates/{cand_id}/bind", json={
                    "ad_account_id": "act_api08",
                    "ad_id": "ad_api08_0",
                    "expected_revision": freeze_rev,
                })
                self.assertEqual(resp.status_code, 200, resp.text)
                data = resp.json()
                self.assertEqual(data["candidate_id"], cand_id)
                self.assertIn("ad_id", data)
        finally:
            self._cleanup(app, dep)

    # --- AS-09: Start ---
    def test_AS_09_start(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "api09")
        client, app, dep = self._client()
        try:
            with client:
                create_resp = client.post("/api/experiments/v2", json={
                    "product_id": PROD_ID,
                    "name": "API Start",
                    "draft_context": _draft_context(),
                })
                exp_id = create_resp.json()["id"]
                rev = 1
                for av in avs:
                    client.post(f"/api/experiments/{exp_id}/entries", json={
                        "asset_version_id": av,
                        "expected_revision": rev,
                    })
                    rev += 1
                freeze_resp = client.post(f"/api/experiments/{exp_id}/freeze/v2", json={
                    "expected_revision": rev,
                })
                freeze_rev = freeze_resp.json()["revision"]
                candidates = freeze_resp.json()["candidates"]
                for i, cand in enumerate(candidates):
                    client.post(f"/api/experiments/{exp_id}/candidates/{cand['id']}/bind", json={
                        "ad_account_id": f"act_api09_{i}",
                        "ad_id": f"ad_api09_{i}",
                        "expected_revision": freeze_rev,
                    })
                resp = client.post(f"/api/experiments/{exp_id}/start", json={
                    "expected_revision": freeze_rev,
                })
                self.assertEqual(resp.status_code, 200, resp.text)
                data = resp.json()
                self.assertEqual(data["state"], "running")
                self.assertIsNotNone(data.get("started_at"))
        finally:
            self._cleanup(app, dep)

    # --- AS-10: Judge ---
    def test_AS_10_judge(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "api10")
        client, app, dep = self._client()
        try:
            with client:
                create_resp = client.post("/api/experiments/v2", json={
                    "product_id": PROD_ID,
                    "name": "API Judge",
                    "draft_context": _draft_context(),
                })
                exp_id = create_resp.json()["id"]
                rev = 1
                for av in avs:
                    client.post(f"/api/experiments/{exp_id}/entries", json={
                        "asset_version_id": av,
                        "expected_revision": rev,
                    })
                    rev += 1
                freeze_resp = client.post(f"/api/experiments/{exp_id}/freeze/v2", json={
                    "expected_revision": rev,
                })
                freeze_rev = freeze_resp.json()["revision"]
                candidates = freeze_resp.json()["candidates"]
                ad_ids = []
                for i, cand in enumerate(candidates):
                    ad_id = f"ad_api10_{i}"
                    ad_ids.append(ad_id)
                    client.post(f"/api/experiments/{exp_id}/candidates/{cand['id']}/bind", json={
                        "ad_account_id": f"act_api10_{i}",
                        "ad_id": ad_id,
                        "expected_revision": freeze_rev,
                    })
                client.post(f"/api/experiments/{exp_id}/start", json={
                    "expected_revision": freeze_rev,
                })
                with tenant_session(CTX) as s:
                    _seed_metrics(s, "api10", ad_ids)
                resp = client.post(f"/api/experiments/{exp_id}/judge/v2", json={
                    "expected_revision": freeze_rev + 1,
                    "as_of": "2026-08-01",
                })
                self.assertEqual(resp.status_code, 200, resp.text)
                data = resp.json()
                self.assertIn("judgement", data)
                self.assertIn("proposal_id", data)
        finally:
            self._cleanup(app, dep)

    # --- AS-11: Confirm ---
    def test_AS_11_confirm(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "api11")
        client, app, dep = self._client()
        try:
            with client:
                create_resp = client.post("/api/experiments/v2", json={
                    "product_id": PROD_ID,
                    "name": "API Confirm",
                    "draft_context": _draft_context(),
                })
                exp_id = create_resp.json()["id"]
                rev = 1
                for av in avs:
                    client.post(f"/api/experiments/{exp_id}/entries", json={
                        "asset_version_id": av,
                        "expected_revision": rev,
                    })
                    rev += 1
                freeze_resp = client.post(f"/api/experiments/{exp_id}/freeze/v2", json={
                    "expected_revision": rev,
                })
                freeze_rev = freeze_resp.json()["revision"]
                candidates = freeze_resp.json()["candidates"]
                ad_ids = []
                for i, cand in enumerate(candidates):
                    ad_id = f"ad_api11_{i}"
                    ad_ids.append(ad_id)
                    client.post(f"/api/experiments/{exp_id}/candidates/{cand['id']}/bind", json={
                        "ad_account_id": f"act_api11_{i}",
                        "ad_id": ad_id,
                        "expected_revision": freeze_rev,
                    })
                start_resp = client.post(f"/api/experiments/{exp_id}/start", json={
                    "expected_revision": freeze_rev,
                })
                run_rev = start_resp.json()["revision"]
                with tenant_session(CTX) as s:
                    _seed_metrics(s, "api11", ad_ids)
                judge_resp = client.post(f"/api/experiments/{exp_id}/judge/v2", json={
                    "expected_revision": run_rev,
                    "as_of": "2026-08-01",
                })
                proposal_id = judge_resp.json()["proposal_id"]
                resp = client.post(f"/api/experiments/{exp_id}/proposals/{proposal_id}/confirm", json={
                    "expected_proposal_sequence": 1,
                })
                self.assertEqual(resp.status_code, 200, resp.text)
                data = resp.json()
                self.assertIn("confirmation_id", data)
                self.assertIn("decisions", data)
                self.assertGreater(len(data["decisions"]), 0)
        finally:
            self._cleanup(app, dep)

    # --- AS-12: Close ---
    def test_AS_12_close(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "api12")
        client, app, dep = self._client()
        try:
            with client:
                create_resp = client.post("/api/experiments/v2", json={
                    "product_id": PROD_ID,
                    "name": "API Close",
                    "draft_context": _draft_context(),
                })
                exp_id = create_resp.json()["id"]
                rev = 1
                for av in avs:
                    client.post(f"/api/experiments/{exp_id}/entries", json={
                        "asset_version_id": av,
                        "expected_revision": rev,
                    })
                    rev += 1
                freeze_resp = client.post(f"/api/experiments/{exp_id}/freeze/v2", json={
                    "expected_revision": rev,
                })
                freeze_rev = freeze_resp.json()["revision"]
                candidates = freeze_resp.json()["candidates"]
                ad_ids = []
                for i, cand in enumerate(candidates):
                    ad_id = f"ad_api12_{i}"
                    ad_ids.append(ad_id)
                    client.post(f"/api/experiments/{exp_id}/candidates/{cand['id']}/bind", json={
                        "ad_account_id": f"act_api12_{i}",
                        "ad_id": ad_id,
                        "expected_revision": freeze_rev,
                    })
                start_resp = client.post(f"/api/experiments/{exp_id}/start", json={
                    "expected_revision": freeze_rev,
                })
                run_rev = start_resp.json()["revision"]
                with tenant_session(CTX) as s:
                    _seed_metrics(s, "api12", ad_ids)
                client.post(f"/api/experiments/{exp_id}/judge/v2", json={
                    "expected_revision": run_rev,
                    "as_of": "2026-08-01",
                })
                resp = client.post(f"/api/experiments/{exp_id}/close/v2", json={
                    "expected_revision": run_rev,
                    "closure_kind": "completed",
                })
                self.assertEqual(resp.status_code, 200, resp.text)
                data = resp.json()
                self.assertEqual(data["state"], "closed")
        finally:
            self._cleanup(app, dep)

    # --- Error paths ---
    def test_create_v2_invalid_context_returns_400(self) -> None:
        client, app, dep = self._client()
        try:
            with client:
                resp = client.post("/api/experiments/v2", json={
                    "product_id": PROD_ID,
                    "name": "Bad Context",
                    "draft_context": {"market": "X", "currency": "Y", "placement": "invalid"},
                })
                self.assertEqual(resp.status_code, 400)
        finally:
            self._cleanup(app, dep)

    def test_add_entry_stale_revision_returns_409(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "api_stale")
        client, app, dep = self._client()
        try:
            with client:
                create_resp = client.post("/api/experiments/v2", json={
                    "product_id": PROD_ID,
                    "name": "Stale Rev",
                    "draft_context": _draft_context(),
                })
                exp_id = create_resp.json()["id"]
                client.post(f"/api/experiments/{exp_id}/entries", json={
                    "asset_version_id": avs[0],
                    "expected_revision": 1,
                })
                resp = client.post(f"/api/experiments/{exp_id}/entries", json={
                    "asset_version_id": avs[1],
                    "expected_revision": 1,
                })
                self.assertEqual(resp.status_code, 409)
        finally:
            self._cleanup(app, dep)

    def test_freeze_insufficient_entries_returns_409(self) -> None:
        client, app, dep = self._client()
        try:
            with client:
                create_resp = client.post("/api/experiments/v2", json={
                    "product_id": PROD_ID,
                    "name": "Freeze No Entries",
                    "draft_context": _draft_context(),
                })
                exp_id = create_resp.json()["id"]
                resp = client.post(f"/api/experiments/{exp_id}/freeze/v2", json={
                    "expected_revision": 1,
                })
                self.assertEqual(resp.status_code, 409)
        finally:
            self._cleanup(app, dep)

    def test_get_nonexistent_returns_404(self) -> None:
        client, app, dep = self._client()
        try:
            with client:
                resp = client.get("/api/experiments/exp_nonexistent_api")
                self.assertEqual(resp.status_code, 404)
        finally:
            self._cleanup(app, dep)

    def test_schema_version_mismatch_returns_409(self) -> None:
        """V2 entry operation on legacy experiment → 409."""
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "api_mismatch")
            legacy_exp = ExperimentRecord(
                id="exp_legacy_api",
                workspace_id=CTX.workspace_id,
                product_id=PROD_ID,
                name="Legacy API",
                state="draft",
                revision=1,
                config_json={"primary_metric": "ctr"},
                metric_contract_hash="z" * 64,
            )
            if s.get(ExperimentRecord, "exp_legacy_api") is None:
                s.add(legacy_exp)
                s.flush()
        client, app, dep = self._client()
        try:
            with client:
                resp = client.post("/api/experiments/exp_legacy_api/entries", json={
                    "asset_version_id": avs[0],
                    "expected_revision": 1,
                })
                self.assertEqual(resp.status_code, 409)
                self.assertIn("schema_version_mismatch", resp.json().get("code", ""))
        finally:
            self._cleanup(app, dep)
