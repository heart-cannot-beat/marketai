"""SQLite parity tests (§5.7.1 SQ-01..SQ-05).

Verifies that the schema-2 domain logic works correctly on the SQLite
dialect. PostgreSQL-only guarantees (triggers, RLS, roles) are NOT tested
here — they require real PostgreSQL (§5.7.2).
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text

from app.adapters.persistence.database import engine, tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    ExperimentRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    PlanRecord,
    ProductRecord,
    WorkspaceRecord,
    new_id,
)
from app.experiments.service import (
    add_entry_v2,
    create_experiment_v2,
    freeze_experiment_v2,
)
from app.platform.canonical import _canonical_bytes, _canonical_json, _sha256
from app.platform.tenancy import TenantContext


CTX = TenantContext(
    workspace_id="ws_sq",
    role="owner",
    initiator_id="user_sq",
    request_id="req_sq",
    trace_id="trace_sq",
)

PROD_ID = "prod_sq"
MIGRATION_REVISION = "t1a2b3c4d5e6"
DOWN_REVISION = "s1a2b3c4d5e6"


def _ensure_workspace(session) -> None:
    if session.get(WorkspaceRecord, CTX.workspace_id) is None:
        session.add(WorkspaceRecord(id=CTX.workspace_id, name="SQ test"))
        session.flush()


def _seed_library_versions(session) -> None:
    for entry_id, ver_id, etype in [
        ("le_pv_sq", "pv_sq", "product"),
        ("le_ov_sq", "ov_sq", "offer"),
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
            id=PROD_ID, workspace_id=CTX.workspace_id, name="P sqlite",
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


def _draft_context(**overrides: object) -> dict:
    base = {
        "market": "US",
        "currency": "USD",
        "placement": "feed",
        "product_version_id": "pv_sq",
        "offer_version_id": "ov_sq",
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


class SQLiteParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_workspace(s)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(
                    id=PROD_ID, workspace_id=CTX.workspace_id, name="P sqlite",
                ))
                s.flush()
            _seed_library_versions(s)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def test_SQ_01_json_round_trip(self) -> None:
        """SQ-01: Insert structured object via SA_JSON, read back, assert structural equality."""
        structured = {
            "nested": {"key": "value", "number": 42},
            "list": [1, 2, "three"],
            "unicode": "中文",
            "empty": {},
            "null_val": None,
        }
        result = create_experiment_v2(
            CTX, product_id=PROD_ID, name="SQ01 JSON",
            draft_context={**_draft_context(), "extra_data": structured},
        )
        exp_id = result["id"]

        with tenant_session(CTX) as s:
            exp = s.get(ExperimentRecord, exp_id)
            config = exp.config_json
            self.assertEqual(config["extra_data"], structured)
            self.assertEqual(config["extra_data"]["nested"]["key"], "value")
            self.assertEqual(config["extra_data"]["list"], [1, 2, "three"])
            self.assertEqual(config["extra_data"]["unicode"], "中文")
            self.assertIsNone(config["extra_data"]["null_val"])

    def test_SQ_02_qc_evidence_hash_via_json(self) -> None:
        """SQ-02: QC evidence hash read via SA_JSON on SQLite, normalize, hash matches."""
        evidence = {
            "rule_id": "qc_001",
            "rule_version": "1.0",
            "check_type": "safety",
            "severity": "blocking",
            "outcome": "passed",
            "evidence_obj": {"score": 95, "threshold": 80},
        }
        expected_hash = _sha256(_canonical_bytes(evidence))

        result = create_experiment_v2(
            CTX, product_id=PROD_ID, name="SQ02 QCHash",
            draft_context={**_draft_context(), "qc_evidence": evidence},
        )
        exp_id = result["id"]

        with tenant_session(CTX) as s:
            exp = s.get(ExperimentRecord, exp_id)
            stored_evidence = exp.config_json["qc_evidence"]
            recomputed = _sha256(_canonical_bytes(stored_evidence))
            self.assertEqual(recomputed, expected_hash)

    def test_SQ_03_migration_upgrade_succeeds(self) -> None:
        """SQ-03: Migration upgrade succeeds on SQLite (verified by current schema state)."""
        inspector = inspect(engine)
        new_tables = [
            "experiment_versions",
            "experiment_version_candidates",
            "experiment_version_candidate_concepts",
            "experiment_version_concept_resolutions",
            "experiment_eligibility_events",
            "experiment_eligibility_projections",
            "experiment_timeline_heads",
            "experiment_timeline_events",
            "experiment_notification_outbox",
            "decision_proposals",
            "decision_proposal_state_events",
            "decision_confirmations",
            "contextual_verdict_projections",
            "experiment_next_round_records",
            "experiment_pattern_candidates",
        ]
        existing = inspector.get_table_names()
        for table in new_tables:
            self.assertIn(table, existing, f"Table {table} missing after upgrade")

    def test_SQ_04_migration_downgrade_possible(self) -> None:
        """SQ-04: Migration downgrade path exists (verify revision chain)."""
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        from alembic.script import ScriptDirectory
        scripts = ScriptDirectory.from_config(cfg)
        rev = scripts.get_revision(MIGRATION_REVISION)
        self.assertIsNotNone(rev, f"Revision {MIGRATION_REVISION} not found")
        self.assertEqual(rev.down_revision, DOWN_REVISION)

    def test_SQ_05_hash_equality_canonical_test_vector(self) -> None:
        """SQ-05: canonical_test_vector produces identical hash on SQLite (pinned value)."""
        vector = {
            "z_string": "hello",
            "a_int": 42,
            "b_decimal": Decimal("1.23"),
            "c_none": None,
            "d_list": [1, 2, 3],
            "e_nested": {"x": Decimal("0.001")},
        }
        h = _sha256(_canonical_bytes(vector))

        canonical_str = _canonical_json(vector)
        expected_canonical = '{"a_int":42,"b_decimal":"1.23","c_none":null,"d_list":[1,2,3],"e_nested":{"x":"0.001"},"z_string":"hello"}'
        self.assertEqual(canonical_str, expected_canonical)

        expected_hash = _sha256(expected_canonical.encode("utf-8"))
        self.assertEqual(h, expected_hash)

        h2 = _sha256(_canonical_bytes(vector))
        self.assertEqual(h, h2)


if __name__ == "__main__":
    unittest.main()
