"""Tests for shared entitlement domain types and value objects (Task 9)."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.entitlements.types import (
    AdmissionRequest,
    AdmissionResult,
    BillableCallSpec,
    CapacityRequest,
    CredentialEncryptionContext,
    CredentialMetadata,
    CustomerNotificationIntent,
    EncryptedEnvelope,
    EntitlementRef,
    EntitlementRequirements,
    ExternalCallOutcome,
    ExecutionMode,
    FrozenQuote,
    InboxOutcome,
    JobSpec,
    NumericRequirement,
    OperationPayload,
    ProviderSelection,
    QuoteBundle,
    ReservationBundle,
    ReservationPlan,
    UsageFact,
)


class TestExecutionMode:
    def test_execution_mode_values(self) -> None:
        assert ExecutionMode.BYOK == "byok"
        assert ExecutionMode.SANDBOX == "sandbox"
        assert ExecutionMode.MOCK == "mock"
        assert len(ExecutionMode) == 3


class TestInboxOutcome:
    def test_inbox_outcome_values(self) -> None:
        assert InboxOutcome.APPLIED == "applied"
        assert InboxOutcome.DUPLICATE == "duplicate"
        assert InboxOutcome.STALE == "stale"
        assert InboxOutcome.CONFLICT == "conflict"
        assert InboxOutcome.VERSION_JUMP_PAUSED == "version_jump_paused"
        assert len(InboxOutcome) == 5


class TestFrozenQuote:
    def test_frozen_quote_is_immutable(self) -> None:
        now = datetime.now(tz=timezone.utc)
        quote = FrozenQuote(
            call_semantic_key_hash="abc123",
            provider="meta",
            model="llama-3",
            execution_mode=ExecutionMode.BYOK,
            quantity=100,
            provider_amount=Decimal("1.50"),
            provider_currency="USD",
            budget_amount=Decimal("1.55"),
            budget_currency="USD",
            exchange_rate=Decimal("1.0"),
            exchange_rate_source="fixed",
            exchange_rate_observed_at=now,
            fx_buffer_bps=50,
            price_rule_version="v1",
            pricing_input_hash="hash1",
            simulated_amount=None,
            simulated_currency=None,
        )
        with pytest.raises((AttributeError, FrozenInstanceError)):
            quote.quantity = 200  # type: ignore[misc]
        assert quote.quantity == 100
        assert quote.provider_amount == Decimal("1.50")


class TestAdmissionRequest:
    def test_admission_request_is_immutable(self) -> None:
        req = AdmissionRequest(
            command_type="generate_ad",
            semantic_key_hash="sem123",
            request_fingerprint_hash="fp456",
            entitlement_requirements=EntitlementRequirements(
                feature_keys=("ad_generation",),
                numeric_requirements=(
                    NumericRequirement(
                        entitlement_key="api_calls",
                        quantity=Decimal("1"),
                        unit="call",
                    ),
                ),
            ),
            provider_selection=ProviderSelection(
                execution_mode=ExecutionMode.BYOK,
                provider="meta",
                model="llama-3",
                binding_scope="workspace",
                credential_binding_id="bind-1",
            ),
            billable_calls=(
                BillableCallSpec(
                    call_semantic_key_hash="call1",
                    provider="meta",
                    model="llama-3",
                    quantity=1,
                ),
            ),
            capacity_requests=(
                CapacityRequest(
                    entitlement_key="api_calls",
                    quantity=Decimal("1"),
                    unit="call",
                    acquisition_phase="reserve_on_admission",
                ),
            ),
            operation_payload=OperationPayload(
                command_type="generate_ad",
                result_resource_type="ad_creative",
                result_resource_id="ad-001",
                payload_version=1,
            ),
            job_specs=(
                JobSpec(
                    job_type="llm_call",
                    resource_type="ad_creative",
                    resource_id="ad-001",
                    payload_json={"prompt": "test"},
                    call_semantic_key_hash="call1",
                ),
            ),
        )
        with pytest.raises((AttributeError, FrozenInstanceError)):
            req.command_type = "other"  # type: ignore[misc]
        assert req.command_type == "generate_ad"


class TestEntitlementRef:
    def test_entitlement_ref_fields(self) -> None:
        now = datetime.now(tz=timezone.utc)
        ref = EntitlementRef(
            entitlement_version=3,
            lease_sequence=7,
            period_start=now,
            period_end=now,
            semantic_hash="hash-abc",
        )
        assert ref.entitlement_version == 3
        assert ref.lease_sequence == 7
        assert ref.period_start == now
        assert ref.period_end == now
        assert ref.semantic_hash == "hash-abc"


class TestCapacityRequest:
    def test_capacity_request_fields(self) -> None:
        cr = CapacityRequest(
            entitlement_key="api_calls",
            quantity=Decimal("5"),
            unit="call",
            acquisition_phase="acquire_on_execution",
        )
        assert cr.entitlement_key == "api_calls"
        assert cr.quantity == Decimal("5")
        assert cr.unit == "call"
        assert cr.acquisition_phase == "acquire_on_execution"


class TestReservationBundle:
    def test_reservation_bundle_fields(self) -> None:
        bundle = ReservationBundle(
            operation_id="op-123",
            spend_reservation_by_call_key={"call1": "res-1", "call2": "res-2"},
            capacity_reservation_ids=("cap-1", "cap-2"),
            job_ids=("job-1", "job-2"),
            guardrail_revision=42,
        )
        assert bundle.operation_id == "op-123"
        assert bundle.spend_reservation_by_call_key == {
            "call1": "res-1",
            "call2": "res-2",
        }
        assert bundle.capacity_reservation_ids == ("cap-1", "cap-2")
        assert bundle.job_ids == ("job-1", "job-2")
        assert bundle.guardrail_revision == 42


class TestCredentialMetadata:
    def test_credential_metadata_fields(self) -> None:
        now = datetime.now(tz=timezone.utc)
        meta = CredentialMetadata(
            credential_version_id="cv-001",
            workspace_id="ws-123",
            credential_key="META_API_KEY",
            version_no=2,
            value_sha256="e3b0c44298fc1c149afbf4c8996fb924",
            created_by="user-456",
            created_at=now,
            revoked_at=None,
        )
        assert meta.credential_version_id == "cv-001"
        assert meta.workspace_id == "ws-123"
        assert meta.credential_key == "META_API_KEY"
        assert meta.version_no == 2
        assert meta.value_sha256 == "e3b0c44298fc1c149afbf4c8996fb924"
        assert meta.created_by == "user-456"
        assert meta.created_at == now
        assert meta.revoked_at is None
