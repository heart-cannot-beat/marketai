"""Tests for GuardrailAdmin, UsageRecorder, CustomerNotificationWriter, and CostReconciler.

Uses in-memory SQLite with all tables created from Base.metadata.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    OperationRecord,
    WorkspaceRecord,
)
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
    SpendGuardrailEventRecord,
    SpendGuardrailRecord,
    SpendReservationRecord,
)
from app.entitlements.cost_reconciler import CostReconciler
from app.entitlements.credentials import CredentialVersionRecord
from app.entitlements.guardrail_admin import GuardrailAdmin
from app.entitlements.notification_writer import CustomerNotificationWriter
from app.entitlements.usage import UsageFact, UsageRecorder, usage_semantic_key
from app.entitlements.usage_models import (
    CustomerNotificationOutboxRecord,
    UsageEventRecord,
)
from app.platform.tenancy import TenantContext

NOW = datetime.now(timezone.utc)

OWNER_CTX = TenantContext(
    workspace_id="ws_test",
    role="owner",
    executor="user",
    initiator_id="user_1",
    initiator_role="owner",
)

MEMBER_CTX = TenantContext(
    workspace_id="ws_test",
    role="member",
    executor="user",
    initiator_id="user_2",
    initiator_role="member",
)


def _make_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _seed_workspace(session, workspace_id="ws_test"):
    """Create a workspace record needed by FK constraints."""
    ws = WorkspaceRecord(
        id=workspace_id,
        name="Test Workspace",
        budget_currency="USD",
        authorization_epoch=0,
        created_at=NOW,
    )
    session.add(ws)
    session.flush()
    return ws


def _seed_guardrail(session, workspace_id="ws_test", revision=1):
    """Create a spend guardrail record for the workspace."""
    record = SpendGuardrailRecord(
        workspace_id=workspace_id,
        revision=revision,
        budget_currency="USD",
        per_operation_limit=Decimal("1.00"),
        rolling_24h_limit=Decimal("5.00"),
        period_limit=Decimal("20.00"),
        outstanding_exposure_limit=Decimal("3.00"),
        platform_policy_version=1,
        created_at=NOW,
        updated_at=NOW,
        updated_by="seed",
    )
    session.add(record)
    session.flush()
    return record


class TestGuardrailAdminGet(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.Session = sessionmaker(bind=self.engine)
        self.session = self.Session()
        _seed_workspace(self.session)
        _seed_guardrail(self.session)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_get_returns_guardrail_config(self):
        admin = GuardrailAdmin()
        result = admin.get(self.session, OWNER_CTX)
        self.assertEqual(result["workspace_id"], "ws_test")
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["budget_currency"], "USD")
        self.assertEqual(result["per_operation_limit"], Decimal("1.00"))
        self.assertEqual(result["rolling_24h_limit"], Decimal("5.00"))
        self.assertEqual(result["period_limit"], Decimal("20.00"))
        self.assertEqual(result["outstanding_exposure_limit"], Decimal("3.00"))
        self.assertEqual(result["platform_policy_version"], 1)


class TestGuardrailAdminPreview(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.Session = sessionmaker(bind=self.engine)
        self.session = self.Session()
        _seed_workspace(self.session)
        _seed_guardrail(self.session)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_preview_update_validates_revision(self):
        admin = GuardrailAdmin()
        new_limits = {
            "per_operation_limit": "2.00",
            "rolling_24h_limit": "8.00",
            "period_limit": "30.00",
            "outstanding_exposure_limit": "4.00",
        }
        with self.assertRaises(ValueError) as cm:
            admin.preview_update(
                self.session,
                OWNER_CTX,
                new_limits=new_limits,
                expected_revision=999,
                mfa_evidence_ref=None,
            )
        self.assertIn("revision mismatch", str(cm.exception))

    def test_preview_update_validates_platform_limits(self):
        admin = GuardrailAdmin()
        # per_operation_limit platform max is 5 (from config defaults)
        new_limits = {
            "per_operation_limit": "999.00",
            "rolling_24h_limit": "8.00",
            "period_limit": "30.00",
            "outstanding_exposure_limit": "4.00",
        }
        with self.assertRaises(ValueError) as cm:
            admin.preview_update(
                self.session,
                OWNER_CTX,
                new_limits=new_limits,
                expected_revision=1,
                mfa_evidence_ref=None,
            )
        self.assertIn("exceeds platform maximum", str(cm.exception))

    def test_preview_update_returns_preview_with_token(self):
        admin = GuardrailAdmin()
        new_limits = {
            "per_operation_limit": "2.00",
            "rolling_24h_limit": "8.00",
            "period_limit": "30.00",
            "outstanding_exposure_limit": "4.00",
        }
        result = admin.preview_update(
            self.session,
            OWNER_CTX,
            new_limits=new_limits,
            expected_revision=1,
            mfa_evidence_ref=None,
        )
        self.assertEqual(result["workspace_id"], "ws_test")
        self.assertEqual(result["current_revision"], 1)
        self.assertIn("old_limits", result)
        self.assertIn("proposed_limits", result)
        self.assertIn("preview_token", result)
        self.assertIsInstance(result["preview_token"], str)
        self.assertGreater(len(result["preview_token"]), 16)

        # Verify proposed limits are parsed Decimals
        self.assertEqual(
            result["proposed_limits"]["per_operation_limit"],
            Decimal("2.00"),
        )

    def test_preview_update_rejects_non_owner(self):
        admin = GuardrailAdmin()
        new_limits = {
            "per_operation_limit": "2.00",
            "rolling_24h_limit": "8.00",
            "period_limit": "30.00",
            "outstanding_exposure_limit": "4.00",
        }
        with self.assertRaises(PermissionError):
            admin.preview_update(
                self.session,
                MEMBER_CTX,
                new_limits=new_limits,
                expected_revision=1,
                mfa_evidence_ref=None,
            )


class TestGuardrailAdminConfirm(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.Session = sessionmaker(bind=self.engine)
        self.session = self.Session()
        _seed_workspace(self.session)
        _seed_guardrail(self.session)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_confirm_update_applies_changes(self):
        admin = GuardrailAdmin()
        new_limits = {
            "per_operation_limit": "3.00",
            "rolling_24h_limit": "10.00",
            "period_limit": "50.00",
            "outstanding_exposure_limit": "5.00",
        }

        # First preview
        preview = admin.preview_update(
            self.session,
            OWNER_CTX,
            new_limits=new_limits,
            expected_revision=1,
            mfa_evidence_ref=None,
        )
        token = preview["preview_token"]

        # Then confirm
        result = admin.confirm_update(
            self.session,
            OWNER_CTX,
            preview_token=token,
            expected_revision=1,
        )

        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["per_operation_limit"], Decimal("3.00"))
        self.assertEqual(result["rolling_24h_limit"], Decimal("10.00"))
        self.assertEqual(result["period_limit"], Decimal("50.00"))
        self.assertEqual(result["outstanding_exposure_limit"], Decimal("5.00"))

        # Verify event was written
        events = (
            self.session.query(SpendGuardrailEventRecord)
            .filter_by(workspace_id="ws_test")
            .all()
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].revision_before, 1)
        self.assertEqual(events[0].revision_after, 2)

    def test_confirm_update_rejects_non_owner(self):
        admin = GuardrailAdmin()
        with self.assertRaises(PermissionError):
            admin.confirm_update(
                self.session,
                MEMBER_CTX,
                preview_token="some_token",
                expected_revision=1,
            )


class TestUsageRecorder(unittest.TestCase):
    """``record_many`` writes registry-shaped rows; ``tests/test_usage_provider_calls.py``
    covers the registry's refusals, duplicates and every settlement point."""

    def setUp(self):
        self.engine = _make_engine()
        self.Session = sessionmaker(bind=self.engine)
        self.session = self.Session()
        _seed_workspace(self.session)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def _fact(self, source_id, occurred_at=NOW, mode="byok"):
        return UsageFact(
            metric_key="provider_calls",
            quantity=Decimal("1"),
            unit="call",
            execution_mode=mode,
            semantic_key_hash=usage_semantic_key("provider_calls", "provider_call_attempt", source_id),
            occurred_at=occurred_at,
            source_resource_type="provider_call_attempt",
            source_resource_id=source_id,
        )

    def test_record_many_creates_usage_event(self):
        result = UsageRecorder().record_many(self.session, OWNER_CTX, [self._fact("pca_1")])
        self.session.commit()

        self.assertEqual((result.written, result.duplicates), (1, 0))
        event = self.session.query(UsageEventRecord).one()
        self.assertTrue(event.id.startswith("ue_"))
        self.assertEqual(event.workspace_id, "ws_test")
        self.assertEqual(event.metric_key, "provider_calls")
        self.assertEqual(event.measurement_kind, "delta")
        self.assertEqual(event.direction, "increase")
        self.assertEqual(event.quantity, Decimal("1"))
        self.assertEqual(event.unit, "call")
        self.assertEqual(event.execution_mode, "byok")
        self.assertEqual((event.source_resource_type, event.source_resource_id),
                         ("provider_call_attempt", "pca_1"))

    def test_record_many_uses_provided_occurred_at(self):
        custom_time = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        UsageRecorder().record_many(self.session, OWNER_CTX, [self._fact("pca_2", custom_time, "sandbox")])
        self.session.commit()

        event = self.session.query(UsageEventRecord).one()
        # SQLite strips timezone info; compare the naive datetime values
        expected = custom_time.replace(tzinfo=None)
        actual = event.occurred_at.replace(tzinfo=None) if event.occurred_at.tzinfo else event.occurred_at
        self.assertEqual(actual, expected)


class TestNotificationWriter(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.Session = sessionmaker(bind=self.engine)
        self.session = self.Session()
        _seed_workspace(self.session)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_enqueue_creates_notification(self):
        writer = CustomerNotificationWriter()
        notif_id = writer.enqueue(
            self.session,
            OWNER_CTX,
            notification_type="spend_warning",
            dimension="rolling_24h",
            threshold=Decimal("0.80"),
            guardrail_revision=1,
            platform_policy_version=1,
            window_kind="rolling",
            window_key="24h",
            crossing_sequence=1,
            deduplication_key="dedup_key_1",
            customer_payload={"message": "You have reached 80% of your limit"},
        )
        self.session.commit()

        self.assertTrue(notif_id.startswith("cn_"))
        record = self.session.get(CustomerNotificationOutboxRecord, notif_id)
        self.assertIsNotNone(record)
        self.assertEqual(record.workspace_id, "ws_test")
        self.assertEqual(record.notification_type, "spend_warning")
        self.assertEqual(record.delivery_state, "pending")
        self.assertEqual(record.attempts, 0)
        self.assertEqual(record.dimension, "rolling_24h")
        self.assertEqual(record.deduplication_key, "dedup_key_1")

    def test_enqueue_deduplication_key_unique(self):
        writer = CustomerNotificationWriter()
        writer.enqueue(
            self.session,
            OWNER_CTX,
            notification_type="spend_warning",
            dimension="rolling_24h",
            threshold=Decimal("0.80"),
            guardrail_revision=1,
            platform_policy_version=1,
            window_kind="rolling",
            window_key="24h",
            crossing_sequence=1,
            deduplication_key="dedup_unique_test",
            customer_payload={"message": "first"},
        )
        self.session.commit()

        with self.assertRaises(IntegrityError):
            writer.enqueue(
                self.session,
                OWNER_CTX,
                notification_type="spend_warning",
                dimension="rolling_24h",
                threshold=Decimal("0.80"),
                guardrail_revision=1,
                platform_policy_version=1,
                window_kind="rolling",
                window_key="24h",
                crossing_sequence=2,
                deduplication_key="dedup_unique_test",
                customer_payload={"message": "duplicate"},
            )


def _seed_cost_prereqs(session, workspace_id="ws_test"):
    """Create all prerequisite records needed by ProviderCallAttemptRecord FKs."""
    # Operation record
    op = OperationRecord(
        id="op_test1",
        workspace_id=workspace_id,
        command_type="generate_image",
        state="running",
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(op)
    session.flush()

    # Job record
    from app.adapters.persistence.models import JobRecord

    job = JobRecord(
        id="job_test1",
        workspace_id=workspace_id,
        operation_id="op_test1",
        initiator_id="user_1",
        initiator_role="owner",
        type="image_generation",
        payload_json={},
        idempotency_key="idem_test1",
        state="running",
        created_at=NOW,
    )
    session.add(job)
    session.flush()

    # Spend reservation
    reservation = SpendReservationRecord(
        id="sr_test1",
        workspace_id=workspace_id,
        operation_id="op_test1",
        job_id="job_test1",
        semantic_key_hash="sr_hash_1",
        state="reserved",
        provider="openai",
        execution_mode="byok",
        provider_currency="USD",
        maximum_provider_amount=Decimal("1.00"),
        budget_currency="USD",
        maximum_budget_amount=Decimal("1.00"),
        outstanding_budget_amount=Decimal("1.00"),
        entitlement_version=1,
        lease_sequence=1,
        guardrail_revision=1,
        platform_policy_version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(reservation)
    session.flush()

    # Credential version (optional FK, but needed if set)
    cred = CredentialVersionRecord(
        id="cv_test1",
        workspace_id=workspace_id,
        credential_key="OPENAI_API_KEY",
        version_no=1,
        provider="openai",
        state="active",
        encrypted_secret=b"encrypted",
        encrypted_data_key=b"datakey",
        encryption_key_id="local:test",
        nonce=b"nonce12345678",
        created_by="user_1",
        created_at=NOW,
    )
    session.add(cred)
    session.flush()

    # Provider call attempt
    attempt = ProviderCallAttemptRecord(
        id="pca_test1",
        workspace_id=workspace_id,
        operation_id="op_test1",
        job_id="job_test1",
        spend_reservation_id="sr_test1",
        call_semantic_key_hash="call_hash_1",
        attempt_no=0,
        execution_mode="byok",
        provider="openai",
        model="gpt-image-1",
        credential_version_id="cv_test1",
        dispatch_state="succeeded",
        prepared_at=NOW,
        updated_at=NOW,
    )
    session.add(attempt)
    session.flush()

    return attempt


class TestCostReconciler(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.Session = sessionmaker(bind=self.engine)
        self.session = self.Session()
        _seed_workspace(self.session)
        self.attempt = _seed_cost_prereqs(self.session)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_reconcile_attempt_creates_reported_cost_entry(self):
        reconciler = CostReconciler()
        entry_id = reconciler.reconcile_attempt(
            self.session,
            OWNER_CTX,
            attempt_id="pca_test1",
            reported_provider_amount=Decimal("0.06"),
            reported_provider_currency="USD",
            evidence_ref="receipt_abc",
        )
        self.session.commit()

        self.assertTrue(entry_id.startswith("pcl_"))
        entry = self.session.get(ProviderCostLedgerEntryRecord, entry_id)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.workspace_id, "ws_test")
        self.assertEqual(entry.entry_type, "reported_cost")
        self.assertEqual(entry.direction, "increase")
        self.assertEqual(entry.provider_amount, Decimal("0.06"))
        self.assertEqual(entry.provider_currency, "USD")
        self.assertEqual(entry.operation_id, "op_test1")
        self.assertEqual(entry.job_id, "job_test1")

    def test_record_adjustment_creates_adjustment_entry(self):
        reconciler = CostReconciler()

        # First, create a reported_cost entry
        original_id = reconciler.reconcile_attempt(
            self.session,
            OWNER_CTX,
            attempt_id="pca_test1",
            reported_provider_amount=Decimal("0.06"),
            reported_provider_currency="USD",
        )
        self.session.commit()

        # Now create an adjustment
        adj_id = reconciler.record_adjustment(
            self.session,
            OWNER_CTX,
            original_entry_id=original_id,
            adjustment_amount=Decimal("-0.02"),
            adjustment_currency="USD",
            reason="provider credit applied",
            actor_id="user_1",
        )
        self.session.commit()

        self.assertTrue(adj_id.startswith("pcl_"))
        adj = self.session.get(ProviderCostLedgerEntryRecord, adj_id)
        self.assertIsNotNone(adj)
        self.assertEqual(adj.entry_type, "adjustment")
        self.assertEqual(adj.direction, "decrease")
        self.assertEqual(adj.provider_amount, Decimal("0.02"))
        self.assertEqual(adj.adjusts_entry_id, original_id)
        self.assertEqual(adj.note, "provider credit applied")


if __name__ == "__main__":
    unittest.main()
