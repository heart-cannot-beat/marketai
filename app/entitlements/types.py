"""Shared domain types and value objects for the entitlements subsystem.

All types are frozen dataclasses -- immutable once constructed.
Consumed by Tasks 10-18 (domain modules).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ExecutionMode(StrEnum):
    """How an external provider call is dispatched."""

    BYOK = "byok"
    SANDBOX = "sandbox"
    MOCK = "mock"


class InboxOutcome(StrEnum):
    """Result of applying an entitlement snapshot to the inbox."""

    APPLIED = "applied"
    DUPLICATE = "duplicate"
    STALE = "stale"
    CONFLICT = "conflict"
    VERSION_JUMP_PAUSED = "version_jump_paused"


# ---------------------------------------------------------------------------
# Provider & entitlement references
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderSelection:
    """Resolved provider + model chosen for an operation."""

    execution_mode: ExecutionMode
    provider: str
    model: str
    binding_scope: str
    credential_binding_id: str | None


@dataclass(frozen=True)
class EntitlementRef:
    """Pointer to the entitlement state that authorized an operation."""

    entitlement_version: int
    lease_sequence: int
    period_start: datetime | None
    period_end: datetime | None
    semantic_hash: str


# ---------------------------------------------------------------------------
# Entitlement requirements
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NumericRequirement:
    """A single numeric entitlement requirement (e.g. API call quota)."""

    entitlement_key: str
    quantity: Decimal
    unit: str


@dataclass(frozen=True)
class EntitlementRequirements:
    """Feature-gate and numeric requirements for an operation."""

    feature_keys: tuple[str, ...]
    numeric_requirements: tuple[NumericRequirement, ...]


# ---------------------------------------------------------------------------
# Quoting / pricing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrozenQuote:
    """Immutable cost quote for a single external call."""

    call_semantic_key_hash: str
    provider: str
    model: str
    execution_mode: ExecutionMode
    quantity: int
    provider_amount: Decimal
    provider_currency: str
    budget_amount: Decimal
    budget_currency: str
    exchange_rate: Decimal
    exchange_rate_source: str
    exchange_rate_observed_at: datetime | None
    fx_buffer_bps: int
    price_rule_version: str
    pricing_input_hash: str
    simulated_amount: Decimal | None
    simulated_currency: str | None


@dataclass(frozen=True)
class QuoteBundle:
    """Aggregated quotes for all calls in an operation."""

    call_quotes: tuple[FrozenQuote, ...]
    total_budget_amount: Decimal


# ---------------------------------------------------------------------------
# Capacity & reservations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapacityRequest:
    """Request to reserve or acquire a unit of entitlement capacity."""

    entitlement_key: str
    quantity: Decimal
    unit: str
    acquisition_phase: str  # "reserve_on_admission" | "acquire_on_execution"


@dataclass(frozen=True)
class GenerationReservationContext:
    """Request-owned generation links, written with the original reservation."""

    run_item_id: str
    preview_confirmation_id: str
    recipe_snapshot_id: str


@dataclass(frozen=True)
class ReservationPlan:
    """Plan describing quotes, capacity and job mapping for an operation."""

    quote_bundle: QuoteBundle
    capacity_requests: tuple[CapacityRequest, ...]
    entitlement_ref: EntitlementRef
    operation_id: str
    job_ids_by_call_key: Mapping[str, str]
    generation_contexts: Mapping[str, GenerationReservationContext] | None = None


@dataclass(frozen=True)
class ReservationBundle:
    """Confirmed reservations after admission succeeds."""

    operation_id: str
    spend_reservation_by_call_key: Mapping[str, str]
    capacity_reservation_ids: tuple[str, ...]
    job_ids: tuple[str, ...]
    guardrail_revision: int


# ---------------------------------------------------------------------------
# Billable calls & jobs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BillableCallSpec:
    """Specification for a single billable external call."""

    call_semantic_key_hash: str
    provider: str
    model: str
    quantity: int


@dataclass(frozen=True)
class JobSpec:
    """Specification for a durable job to be created."""

    job_type: str
    resource_type: str
    resource_id: str
    payload_json: dict
    call_semantic_key_hash: str


@dataclass(frozen=True)
class OperationPayload:
    """Metadata describing the command that produced an operation."""

    command_type: str
    result_resource_type: str | None
    result_resource_id: str | None
    payload_version: int


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionRequest:
    """Full admission request submitted to the entitlement gate."""

    command_type: str
    semantic_key_hash: str
    request_fingerprint_hash: str
    entitlement_requirements: EntitlementRequirements
    provider_selection: ProviderSelection
    billable_calls: tuple[BillableCallSpec, ...]
    capacity_requests: tuple[CapacityRequest, ...]
    operation_payload: OperationPayload
    job_specs: tuple[JobSpec, ...]


@dataclass(frozen=True)
class AdmissionResult:
    """Outcome of a successful admission."""

    operation_id: str
    entitlement_ref: EntitlementRef
    reservation_bundle: ReservationBundle
    replayed: bool


# ---------------------------------------------------------------------------
# External call outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExternalCallOutcome:
    """Outcome evidence for a single external provider call."""

    attempt_id: str
    call_semantic_key_hash: str
    dispatch_certainty: str  # "not_dispatched" | "may_have_dispatched" | "dispatched"
    success: bool
    provider_request_id: str | None
    error_code: str | None
    error_detail: str | None


# ---------------------------------------------------------------------------
# Notifications & usage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CustomerNotificationIntent:
    """Intent to send a customer-facing notification."""

    notification_type: str
    channel: str
    deduplication_key: str
    payload: dict


@dataclass(frozen=True)
class UsageFact:
    """Immutable usage event for metering and billing."""

    semantic_key_hash: str
    operation_id: str
    job_id: str
    event_type: str
    resource_type: str
    quantity: int
    metadata: dict | None


# ---------------------------------------------------------------------------
# Credential management
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CredentialMetadata:
    """Non-secret metadata for a stored BYOK credential version."""

    credential_version_id: str
    workspace_id: str
    credential_key: str
    version_no: int
    value_sha256: str
    created_by: str
    created_at: datetime
    revoked_at: datetime | None
    # What the Settings page shows an Owner about a stored key -- all
    # non-secret columns of ``credential_versions`` (the API role reads
    # nothing else). Defaulted so earlier call sites keep constructing.
    provider: str = ""
    display_label: str | None = None
    state: str = "active"
    rotated_at: datetime | None = None


@dataclass(frozen=True)
class EncryptedEnvelope:
    """Encrypted credential value with envelope metadata."""

    encrypted_value: bytes
    encryption_key_id: str
    encryption_algorithm: str
    nonce: bytes
    aad_context: str


@dataclass(frozen=True)
class CredentialEncryptionContext:
    """AAD context fields for credential encryption."""

    workspace_id: str
    credential_key: str
    version_no: int
