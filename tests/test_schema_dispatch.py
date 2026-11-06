"""Schema-version dispatch tests (SD-01..SD-07).

Verifies that legacy (NULL schema_version) and schema-2 experiments coexist,
each using the correct code path, and that cross-version calls are rejected.
"""
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
    AssetRecord,
    AssetVersionRecord,
    ExperimentEntryRecord,
    ExperimentRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    PlanRecord,
    ProductRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.experiments import service as experiment_service
from app.experiments.service import (
    ExperimentConflict,
    ExperimentError,
    SchemaVersionMismatch,
    _require_schema2,
    add_entry_v2,
    create_experiment_v2,
    freeze_experiment_v2,
    get_experiment,
)
from app.platform.tenancy import TenantContext

CTX = TenantContext(
    workspace_id="ws_sd",
    role="owner",
    initiator_id="user_sd",
    request_id="req_sd",
    trace_id="trace_sd",
)

PROD_ID = "prod_sd"


def _draft_context(**overrides: object) -> dict:
    base = {
        "market": "US",
        "currency": "USD",
        "placement": "feed",
        "product_version_id": "pv_sd",
        "offer_version_id": "ov_sd",
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
    ws = session.get(WorkspaceRecord, CTX.workspace_id)
    if ws is None:
        session.add(WorkspaceRecord(id=CTX.workspace_id, name="SD test"))
        session.flush()


def _seed_library_versions(session) -> None:
    for entry_id, ver_id, etype in [
        ("le_pv_sd", "pv_sd", "product"),
        ("le_ov_sd", "ov_sd", "offer"),
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
        session.add(ProductRecord(
            id=PROD_ID, workspace_id=CTX.workspace_id, name="P dispatch",
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
    return av_ids


class SchemaDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_workspace(s)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(
                    id=PROD_ID, workspace_id=CTX.workspace_id, name="P dispatch",
                ))
                s.flush()
            _seed_library_versions(s)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def test_SD_01_legacy_create_still_works(self) -> None:
        """SD-01: Legacy create_experiment still works post-migration."""
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "sd01")

        exp_id = experiment_service.create_experiment(
            CTX,
            product_id=PROD_ID,
            name="Legacy SD-01",
            config_snapshot={
                "primary_metric": "ctr",
                "ctr_threshold": "0.01",
                "alpha": "0.05",
                "bootstrap_samples": 2000,
            },
            metric_contract_hash="a" * 64,
            entries=[
                {"asset_version_id": avs[0]},
                {"asset_version_id": avs[1]},
            ],
        )
        self.assertTrue(exp_id.startswith("exp_"))

        with tenant_session(CTX) as s:
            exp = s.get(ExperimentRecord, exp_id)
            self.assertIsNone(exp.record_schema_version)
            self.assertEqual(exp.state, "draft")

    def test_SD_02_legacy_experiment_uses_legacy_read_path(self) -> None:
        """SD-02: Legacy experiment returns legacy dict fields via get_experiment."""
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "sd02")

        exp_id = experiment_service.create_experiment(
            CTX,
            product_id=PROD_ID,
            name="Legacy SD-02",
            config_snapshot={
                "primary_metric": "ctr",
                "ctr_threshold": "0.01",
                "alpha": "0.05",
                "bootstrap_samples": 2000,
            },
            metric_contract_hash="b" * 64,
            entries=[
                {"asset_version_id": avs[0]},
                {"asset_version_id": avs[1]},
            ],
        )

        result = get_experiment(CTX, exp_id)
        self.assertIn("product_id", result)
        self.assertIn("metric_contract_hash", result)
        self.assertIn("config", result)
        self.assertNotIn("record_schema_version", result)
        self.assertNotIn("draft_context", result)

    def test_SD_03_schema2_experiment_uses_schema2_read_path(self) -> None:
        """SD-03: Schema-2 experiment returns schema-2 fields."""
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "sd03")

        result = create_experiment_v2(
            CTX,
            product_id=PROD_ID,
            name="Schema2 SD-03",
            draft_context=_draft_context(),
        )
        self.assertEqual(result["record_schema_version"], 2)
        self.assertIn("draft_context", result)
        self.assertEqual(result["state"], "draft")

    def test_SD_04_schema2_function_on_legacy_experiment(self) -> None:
        """SD-04: Calling a v2 function on a legacy experiment raises SchemaVersionMismatch."""
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "sd04")

        exp_id = experiment_service.create_experiment(
            CTX,
            product_id=PROD_ID,
            name="Legacy SD-04",
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
            add_entry_v2(
                CTX, exp_id,
                asset_version_id=avs[0],
                expected_revision=1,
            )

    def test_SD_05_legacy_freeze_produces_legacy_snapshot(self) -> None:
        """SD-05: Legacy freeze_experiment on a schema-2 experiment produces a v1-shaped snapshot."""
        result = create_experiment_v2(
            CTX,
            product_id=PROD_ID,
            name="Schema2 SD-05",
            draft_context=_draft_context(),
        )
        exp_id = result["id"]
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "sd05")
        add_entry_v2(CTX, exp_id, asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp_id, asset_version_id=avs[1], expected_revision=2)

        frozen = experiment_service.freeze_experiment(CTX, exp_id, expected_revision=3)
        self.assertEqual(frozen["state"], "frozen")
        with tenant_session(CTX) as s:
            exp = s.get(ExperimentRecord, exp_id)
            snapshot = exp.frozen_snapshot_json
            self.assertEqual(snapshot["schema_version"], 1)
            self.assertNotIn("version_id", snapshot)

    def test_SD_06_set_verdict_retired(self) -> None:
        """SD-06: The service module has no set_verdict function."""
        self.assertFalse(hasattr(experiment_service, "set_verdict"))

    def test_SD_07_legacy_and_schema2_coexist(self) -> None:
        """SD-07: Both legacy and schema-2 experiments in same workspace are accessible."""
        with tenant_session(CTX) as s:
            avs_legacy = _seed_asset_versions(s, "sd07leg")
            avs_v2 = _seed_asset_versions(s, "sd07v2")

        legacy_id = experiment_service.create_experiment(
            CTX,
            product_id=PROD_ID,
            name="Legacy SD-07",
            config_snapshot={
                "primary_metric": "ctr",
                "ctr_threshold": "0.01",
                "alpha": "0.05",
                "bootstrap_samples": 2000,
            },
            metric_contract_hash="e" * 64,
            entries=[
                {"asset_version_id": avs_legacy[0]},
                {"asset_version_id": avs_legacy[1]},
            ],
        )

        v2_result = create_experiment_v2(
            CTX,
            product_id=PROD_ID,
            name="Schema2 SD-07",
            draft_context=_draft_context(),
        )
        v2_id = v2_result["id"]

        legacy_read = get_experiment(CTX, legacy_id)
        self.assertEqual(legacy_read["state"], "draft")
        self.assertIn("product_id", legacy_read)

        with tenant_session(CTX) as s:
            v2_exp = s.get(ExperimentRecord, v2_id)
            self.assertEqual(v2_exp.record_schema_version, 2)
            self.assertEqual(v2_exp.state, "draft")

            legacy_exp = s.get(ExperimentRecord, legacy_id)
            self.assertIsNone(legacy_exp.record_schema_version)
            self.assertEqual(legacy_exp.state, "draft")

        self.assertNotEqual(legacy_id, v2_id)
