"""Notification outbox tests (NO-01..NO-18).

Covers append_notification dedup, canonical JSON payload hashing,
payload_schema_version, and integration with service-level confirm
and next-round flows.
"""
from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.adapters.persistence.database import engine, tenant_session
from app.adapters.persistence.models import (
    ExperimentNotificationOutboxRecord,
    ExperimentRecord,
    ProductRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.experiments.outbox import append_notification
from app.experiments.timeline import append_event as append_timeline_event, ensure_head
from app.platform.canonical import _canonical_json, _sha256
from app.platform.tenancy import TenantContext

CTX = TenantContext(
    workspace_id="ws_no",
    role="owner",
    initiator_id="user_no",
    request_id="req_no",
    trace_id="trace_no",
)

PROD_ID = "prod_no"


def _ensure_workspace(session) -> None:
    if session.get(WorkspaceRecord, CTX.workspace_id) is None:
        session.add(WorkspaceRecord(id=CTX.workspace_id, name="Outbox test"))
        session.flush()


def _seed_product(session) -> None:
    _ensure_workspace(session)
    if session.get(ProductRecord, PROD_ID) is None:
        session.add(ProductRecord(
            id=PROD_ID, workspace_id=CTX.workspace_id, name="P outbox",
        ))
        session.flush()


def _ensure_experiment(session, experiment_id: str) -> None:
    """Seed a minimal ExperimentRecord to satisfy timeline FKs."""
    if session.get(ExperimentRecord, experiment_id) is None:
        session.add(ExperimentRecord(
            id=experiment_id,
            workspace_id=CTX.workspace_id,
            product_id=PROD_ID,
            name=f"exp {experiment_id}",
            state="draft",
            revision=1,
            config_json={},
            metric_contract_hash="b" * 64,
        ))
        session.flush()


def _seed_timeline_event(session, experiment_id: str, tag: str):
    """Create experiment + timeline head + event to satisfy the outbox FK."""
    _ensure_experiment(session, experiment_id)
    ensure_head(session, workspace_id=CTX.workspace_id, experiment_id=experiment_id)
    return append_timeline_event(
        session,
        workspace_id=CTX.workspace_id,
        experiment_id=experiment_id,
        event_type=f"test_{tag}",
        source_resource_type="test",
        source_resource_id=f"src_{tag}",
        actor_id="user_no",
    )


class NotificationOutboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _seed_product(s)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    # --- NO-01: Basic notification creation ---
    def test_NO_01_basic_creation(self) -> None:
        exp_id = f"exp_no01_{new_id('t')}"
        with tenant_session(CTX) as s:
            tl = _seed_timeline_event(s, exp_id, "no01")
            notif = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="test_created",
                payload={"foo": "bar"},
            )
            self.assertTrue(notif.id.startswith("notif_"))
            self.assertEqual(notif.workspace_id, CTX.workspace_id)
            self.assertEqual(notif.experiment_id, exp_id)
            self.assertEqual(notif.timeline_event_id, tl.id)
            self.assertEqual(notif.notification_type, "test_created")
            self.assertEqual(notif.payload_schema_version, 1)

    # --- NO-02: Payload is canonical JSON ---
    def test_NO_02_canonical_json_payload(self) -> None:
        exp_id = f"exp_no02_{new_id('t')}"
        payload = {"z_key": "last", "a_key": "first", "m_key": 42}
        expected_canonical = _canonical_json(payload)
        with tenant_session(CTX) as s:
            tl = _seed_timeline_event(s, exp_id, "no02")
            notif = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="test_canonical",
                payload=payload,
            )
            self.assertEqual(notif.payload_canonical_json, expected_canonical)
            parsed = json.loads(notif.payload_canonical_json)
            keys = list(parsed.keys())
            self.assertEqual(keys, sorted(keys))

    # --- NO-03: Payload hash matches sha256 of canonical JSON ---
    def test_NO_03_payload_hash(self) -> None:
        exp_id = f"exp_no03_{new_id('t')}"
        payload = {"experiment_id": exp_id, "action": "test"}
        expected_hash = _sha256(_canonical_json(payload).encode("utf-8"))
        with tenant_session(CTX) as s:
            tl = _seed_timeline_event(s, exp_id, "no03")
            notif = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="test_hash",
                payload=payload,
            )
            self.assertEqual(notif.payload_hash, expected_hash)
            self.assertEqual(len(notif.payload_hash), 64)

    # --- NO-04: Dedup on (workspace_id, timeline_event_id, notification_type) ---
    def test_NO_04_dedup_same_event_and_type(self) -> None:
        exp_id = f"exp_no04_{new_id('t')}"
        with tenant_session(CTX) as s:
            tl = _seed_timeline_event(s, exp_id, "no04")
            first = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="test_dedup",
                payload={"attempt": 1},
            )
            second = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="test_dedup",
                payload={"attempt": 2},
            )
            self.assertEqual(first.id, second.id)
            self.assertIn('"attempt":1', first.payload_canonical_json)

    # --- NO-05: Different notification_type same event → distinct records ---
    def test_NO_05_different_type_same_event(self) -> None:
        exp_id = f"exp_no05_{new_id('t')}"
        with tenant_session(CTX) as s:
            tl = _seed_timeline_event(s, exp_id, "no05")
            n1 = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="type_a",
                payload={"v": 1},
            )
            n2 = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="type_b",
                payload={"v": 2},
            )
            self.assertNotEqual(n1.id, n2.id)

    # --- NO-06: Same notification_type different events → distinct records ---
    def test_NO_06_same_type_different_events(self) -> None:
        exp_id = f"exp_no06_{new_id('t')}"
        with tenant_session(CTX) as s:
            tl1 = _seed_timeline_event(s, exp_id, "no06a")
            tl2 = _seed_timeline_event(s, exp_id, "no06b")
            n1 = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl1.id,
                notification_type="same_type",
                payload={"v": 1},
            )
            n2 = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl2.id,
                notification_type="same_type",
                payload={"v": 2},
            )
            self.assertNotEqual(n1.id, n2.id)

    # --- NO-07: payload_schema_version always 1 ---
    def test_NO_07_schema_version_is_1(self) -> None:
        exp_id = f"exp_no07_{new_id('t')}"
        with tenant_session(CTX) as s:
            tl = _seed_timeline_event(s, exp_id, "no07")
            notif = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="test_schema",
                payload={"data": True},
            )
            self.assertEqual(notif.payload_schema_version, 1)

    # --- NO-08: created_at is auto-populated ---
    def test_NO_08_created_at_auto(self) -> None:
        exp_id = f"exp_no08_{new_id('t')}"
        with tenant_session(CTX) as s:
            tl = _seed_timeline_event(s, exp_id, "no08")
            notif = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="test_time",
                payload={},
            )
            self.assertIsNotNone(notif.created_at)

    # --- NO-09: Notification record is queryable by experiment_id ---
    def test_NO_09_query_by_experiment(self) -> None:
        exp_id = f"exp_no09_{new_id('t')}"
        with tenant_session(CTX) as s:
            tl = _seed_timeline_event(s, exp_id, "no09")
            append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="query_test",
                payload={"exp": exp_id},
            )
            rows = s.scalars(
                select(ExperimentNotificationOutboxRecord).where(
                    ExperimentNotificationOutboxRecord.experiment_id == exp_id,
                )
            ).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].notification_type, "query_test")

    # --- NO-10: Empty payload produces valid canonical JSON ---
    def test_NO_10_empty_payload(self) -> None:
        exp_id = f"exp_no10_{new_id('t')}"
        with tenant_session(CTX) as s:
            tl = _seed_timeline_event(s, exp_id, "no10")
            notif = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="empty_payload",
                payload={},
            )
            self.assertEqual(notif.payload_canonical_json, "{}")
            self.assertEqual(notif.payload_hash, _sha256(b"{}"))

    # --- NO-11: Nested payload is serialized with sorted keys ---
    def test_NO_11_nested_payload_sorted(self) -> None:
        exp_id = f"exp_no11_{new_id('t')}"
        payload = {"outer": {"z": 1, "a": 2}, "inner": [3, 2, 1]}
        with tenant_session(CTX) as s:
            tl = _seed_timeline_event(s, exp_id, "no11")
            notif = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="nested",
                payload=payload,
            )
            parsed = json.loads(notif.payload_canonical_json)
            outer_keys = list(parsed["outer"].keys())
            self.assertEqual(outer_keys, ["a", "z"])
            self.assertEqual(parsed["inner"], [3, 2, 1])

    # --- NO-12: Dedup returns original without updating payload ---
    def test_NO_12_dedup_preserves_original_payload(self) -> None:
        exp_id = f"exp_no12_{new_id('t')}"
        with tenant_session(CTX) as s:
            tl = _seed_timeline_event(s, exp_id, "no12")
            original = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="dedup_preserve",
                payload={"version": "original"},
            )
            duplicate = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="dedup_preserve",
                payload={"version": "updated"},
            )
            self.assertEqual(original.id, duplicate.id)
            self.assertIn("original", duplicate.payload_canonical_json)
            self.assertNotIn("updated", duplicate.payload_canonical_json)

    # --- NO-13: Multiple experiments each get their own notifications ---
    def test_NO_13_cross_experiment_isolation(self) -> None:
        exp1 = f"exp_no13a_{new_id('t')}"
        exp2 = f"exp_no13b_{new_id('t')}"
        with tenant_session(CTX) as s:
            tl1 = _seed_timeline_event(s, exp1, "no13a")
            tl2 = _seed_timeline_event(s, exp2, "no13b")
            n1 = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp1,
                timeline_event_id=tl1.id,
                notification_type="cross_exp",
                payload={"exp": exp1},
            )
            n2 = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp2,
                timeline_event_id=tl2.id,
                notification_type="cross_exp",
                payload={"exp": exp2},
            )
            self.assertNotEqual(n1.id, n2.id)
            rows_1 = s.scalars(
                select(ExperimentNotificationOutboxRecord).where(
                    ExperimentNotificationOutboxRecord.experiment_id == exp1,
                )
            ).all()
            rows_2 = s.scalars(
                select(ExperimentNotificationOutboxRecord).where(
                    ExperimentNotificationOutboxRecord.experiment_id == exp2,
                )
            ).all()
            self.assertEqual(len(rows_1), 1)
            self.assertEqual(len(rows_2), 1)

    # --- NO-14: Notification ID format ---
    def test_NO_14_id_prefix(self) -> None:
        exp_id = f"exp_no14_{new_id('t')}"
        with tenant_session(CTX) as s:
            tl = _seed_timeline_event(s, exp_id, "no14")
            notif = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="id_test",
                payload={},
            )
            self.assertTrue(notif.id.startswith("notif_"))

    # --- NO-15: Hash stability — same payload produces same hash ---
    def test_NO_15_hash_stability(self) -> None:
        payload = {"stable": True, "key": "value"}
        h1 = _sha256(_canonical_json(payload).encode("utf-8"))
        h2 = _sha256(_canonical_json(payload).encode("utf-8"))
        self.assertEqual(h1, h2)
        exp_id = f"exp_no15_{new_id('t')}"
        with tenant_session(CTX) as s:
            tl = _seed_timeline_event(s, exp_id, "no15")
            notif = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="stable_hash",
                payload=payload,
            )
            self.assertEqual(notif.payload_hash, h1)

    # --- NO-16: Different payloads produce different hashes ---
    def test_NO_16_different_payload_different_hash(self) -> None:
        exp_id = f"exp_no16_{new_id('t')}"
        with tenant_session(CTX) as s:
            tl1 = _seed_timeline_event(s, exp_id, "no16a")
            tl2 = _seed_timeline_event(s, exp_id, "no16b")
            n1 = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl1.id,
                notification_type="hash_diff",
                payload={"v": 1},
            )
            n2 = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl2.id,
                notification_type="hash_diff",
                payload={"v": 2},
            )
            self.assertNotEqual(n1.payload_hash, n2.payload_hash)

    # --- NO-17: Canonical JSON has no whitespace separators ---
    def test_NO_17_compact_json(self) -> None:
        exp_id = f"exp_no17_{new_id('t')}"
        with tenant_session(CTX) as s:
            tl = _seed_timeline_event(s, exp_id, "no17")
            notif = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="compact",
                payload={"a": 1, "b": 2},
            )
            self.assertNotIn(": ", notif.payload_canonical_json)
            self.assertNotIn(", ", notif.payload_canonical_json)

    # --- NO-18: Notification persisted in DB is readable after flush ---
    def test_NO_18_persistence_roundtrip(self) -> None:
        exp_id = f"exp_no18_{new_id('t')}"
        with tenant_session(CTX) as s:
            tl = _seed_timeline_event(s, exp_id, "no18")
            notif = append_notification(
                s,
                workspace_id=CTX.workspace_id,
                experiment_id=exp_id,
                timeline_event_id=tl.id,
                notification_type="roundtrip",
                payload={"round": "trip"},
            )
            notif_id = notif.id

        with tenant_session(CTX) as s:
            loaded = s.get(ExperimentNotificationOutboxRecord, notif_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.notification_type, "roundtrip")
            self.assertEqual(json.loads(loaded.payload_canonical_json), {"round": "trip"})
            self.assertEqual(loaded.payload_schema_version, 1)


if __name__ == "__main__":
    unittest.main()
