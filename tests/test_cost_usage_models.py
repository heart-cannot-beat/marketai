"""Structure tests for credential, cost, usage, and notification SQLAlchemy models.

Validates table names, required columns, CHECK constraints, and column presence
on extended records (WorkspaceRecord, OperationRecord, JobRecord).
"""
import pytest

from app.adapters.persistence.models import (
    JobRecord,
    OperationRecord,
    WorkspaceRecord,
)
from app.entitlements.credentials import (
    CredentialBindingRecord,
    CredentialVersionRecord,
)
from app.entitlements.cost_models import (
    CapacityReservationRecord,
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
    SpendGuardrailEventRecord,
    SpendGuardrailRecord,
    SpendReservationRecord,
)
from app.entitlements.usage_models import (
    CustomerNotificationOutboxRecord,
    GuardrailNotificationStateRecord,
    UsageEventRecord,
)
from app.entitlements.models import (
    EntitlementSnapshotInboxRecord,
)


def _column_names(model):
    return {c.name for c in model.__table__.columns}


def _check_constraint_names(model):
    """Return the logical CHECK constraint names, stripping SQLAlchemy's
    auto-generated ``ck_<tablename>_`` prefix when present."""
    names = set()
    prefix = f"ck_{model.__tablename__}_"
    for c in model.__table__.constraints:
        if c.__class__.__name__ == "CheckConstraint" and c.name:
            name = c.name
            if name.startswith(prefix):
                name = name[len(prefix):]
            names.add(name)
    return names


# ---------------------------------------------------------------------------
# Table name checks
# ---------------------------------------------------------------------------

class TestTableNames:
    def test_credential_versions_table_name(self):
        assert CredentialVersionRecord.__tablename__ == "credential_versions"

    def test_credential_bindings_table_name(self):
        assert CredentialBindingRecord.__tablename__ == "credential_bindings"

    def test_spend_guardrails_table_name(self):
        assert SpendGuardrailRecord.__tablename__ == "spend_guardrails"

    def test_spend_guardrail_events_table_name(self):
        assert SpendGuardrailEventRecord.__tablename__ == "spend_guardrail_events"

    def test_spend_reservations_table_name(self):
        assert SpendReservationRecord.__tablename__ == "spend_reservations"

    def test_capacity_reservations_table_name(self):
        assert CapacityReservationRecord.__tablename__ == "capacity_reservations"

    def test_provider_call_attempts_table_name(self):
        assert ProviderCallAttemptRecord.__tablename__ == "provider_call_attempts"

    def test_provider_cost_ledger_table_name(self):
        assert ProviderCostLedgerEntryRecord.__tablename__ == "provider_cost_ledger"

    def test_usage_events_table_name(self):
        assert UsageEventRecord.__tablename__ == "usage_events"

    def test_customer_notification_outbox_table_name(self):
        assert CustomerNotificationOutboxRecord.__tablename__ == "customer_notification_outbox"

    def test_guardrail_notification_states_table_name(self):
        assert GuardrailNotificationStateRecord.__tablename__ == "guardrail_notification_states"


# ---------------------------------------------------------------------------
# WorkspaceRecord extensions
# ---------------------------------------------------------------------------

class TestWorkspaceRecordExtensions:
    def test_workspace_has_lifecycle(self):
        assert "lifecycle" in _column_names(WorkspaceRecord)

    def test_workspace_has_budget_currency(self):
        assert "budget_currency" in _column_names(WorkspaceRecord)


# ---------------------------------------------------------------------------
# OperationRecord extensions
# ---------------------------------------------------------------------------

class TestOperationRecordExtensions:
    def test_operation_has_semantic_key_hash(self):
        assert "semantic_key_hash" in _column_names(OperationRecord)

    def test_operation_has_request_fingerprint_hash(self):
        assert "request_fingerprint_hash" in _column_names(OperationRecord)

    def test_operation_has_admission_contract_version(self):
        assert "admission_contract_version" in _column_names(OperationRecord)


# ---------------------------------------------------------------------------
# JobRecord extensions
# ---------------------------------------------------------------------------

class TestJobRecordExtensions:
    def test_job_has_operation_id(self):
        assert "operation_id" in _column_names(JobRecord)

    def test_job_has_entitlement_version_at_enqueue(self):
        assert "entitlement_version_at_enqueue" in _column_names(JobRecord)

    def test_job_has_execution_contract_version(self):
        assert "execution_contract_version" in _column_names(JobRecord)


# ---------------------------------------------------------------------------
# CredentialVersionRecord required columns (spec section 2.4)
# ---------------------------------------------------------------------------

class TestCredentialVersionRecordColumns:
    def test_required_columns(self):
        columns = _column_names(CredentialVersionRecord)
        required = {
            "id", "workspace_id", "credential_key", "version_no",
            "provider", "display_label", "state",
            "encrypted_secret", "encrypted_data_key",
            "encryption_key_id", "encryption_algorithm",
            "nonce", "aad_schema_version",
            "created_by", "created_at",
            "rotated_at", "rotated_by",
            "revoked_at", "revoked_by", "revoke_reason",
        }
        assert required.issubset(columns), f"Missing: {required - columns}"

    def test_state_check_constraint(self):
        names = _check_constraint_names(CredentialVersionRecord)
        assert "credential_version_state_valid" in names

    def test_version_no_check_constraint(self):
        names = _check_constraint_names(CredentialVersionRecord)
        assert "credential_version_no_positive" in names


# ---------------------------------------------------------------------------
# CredentialBindingRecord required columns (spec section 2.5)
# ---------------------------------------------------------------------------

class TestCredentialBindingRecordColumns:
    def test_required_columns(self):
        columns = _column_names(CredentialBindingRecord)
        required = {
            "id", "workspace_id", "binding_scope", "provider",
            "credential_version_id", "revision",
            "bound_by", "created_at", "updated_at",
        }
        assert required.issubset(columns), f"Missing: {required - columns}"


# ---------------------------------------------------------------------------
# SpendGuardrailRecord required columns (spec section 2.7)
# ---------------------------------------------------------------------------

class TestSpendGuardrailRecordColumns:
    def test_required_columns(self):
        columns = _column_names(SpendGuardrailRecord)
        required = {
            "workspace_id", "revision", "budget_currency",
            "per_operation_limit", "rolling_24h_limit",
            "period_limit", "outstanding_exposure_limit",
            "platform_policy_version",
            "created_at", "updated_at", "updated_by",
        }
        assert required.issubset(columns), f"Missing: {required - columns}"


# ---------------------------------------------------------------------------
# SpendGuardrailEventRecord required columns (spec section 2.8)
# ---------------------------------------------------------------------------

class TestSpendGuardrailEventRecordColumns:
    def test_required_columns(self):
        columns = _column_names(SpendGuardrailEventRecord)
        required = {
            "id", "workspace_id",
            "revision_before", "revision_after",
            "old_values_json", "new_values_json",
            "old_budget_currency", "new_budget_currency",
            "reason", "actor_id",
            "audit_event_id", "mfa_evidence_ref",
            "created_at",
        }
        assert required.issubset(columns), f"Missing: {required - columns}"


# ---------------------------------------------------------------------------
# SpendReservationRecord required columns (spec section 2.9)
# ---------------------------------------------------------------------------

class TestSpendReservationRecordColumns:
    def test_required_columns(self):
        columns = _column_names(SpendReservationRecord)
        required = {
            "id", "workspace_id", "operation_id", "job_id",
            "semantic_key_hash", "state",
            "provider", "execution_mode",
            "provider_currency", "maximum_provider_amount",
            "budget_currency", "maximum_budget_amount",
            "outstanding_budget_amount",
            "period_start", "period_end",
            "entitlement_version", "lease_sequence",
            "guardrail_revision", "platform_policy_version",
            "created_at", "updated_at", "settled_at", "released_at",
        }
        assert required.issubset(columns), f"Missing: {required - columns}"

    def test_state_check_constraint(self):
        names = _check_constraint_names(SpendReservationRecord)
        assert "spend_reservation_state_valid" in names

    def test_outstanding_le_maximum_check_constraint(self):
        names = _check_constraint_names(SpendReservationRecord)
        assert "spend_reservation_outstanding_le_maximum" in names


# ---------------------------------------------------------------------------
# CapacityReservationRecord required columns (spec section 2.10)
# ---------------------------------------------------------------------------

class TestCapacityReservationRecordColumns:
    def test_required_columns(self):
        columns = _column_names(CapacityReservationRecord)
        required = {
            "id", "workspace_id", "operation_id", "job_id",
            "entitlement_key", "quantity", "unit",
            "acquisition_phase", "semantic_key_hash", "state",
            "entitlement_version", "lease_sequence",
            "expires_at",
            "created_at", "updated_at", "resolved_at",
        }
        assert required.issubset(columns), f"Missing: {required - columns}"

    def test_state_check_constraint(self):
        names = _check_constraint_names(CapacityReservationRecord)
        assert "capacity_reservation_state_valid" in names

    def test_phase_check_constraint(self):
        names = _check_constraint_names(CapacityReservationRecord)
        assert "capacity_reservation_phase_valid" in names


# ---------------------------------------------------------------------------
# ProviderCallAttemptRecord required columns (spec section 3.1)
# ---------------------------------------------------------------------------

class TestProviderCallAttemptRecordColumns:
    def test_required_columns(self):
        columns = _column_names(ProviderCallAttemptRecord)
        required = {
            "id", "workspace_id", "operation_id", "job_id",
            "spend_reservation_id", "call_semantic_key_hash", "attempt_no",
            "execution_mode", "provider", "model",
            "request_fingerprint_hash", "provider_idempotency_key",
            "credential_version_id",
            "dispatch_state",
            "provider_request_id", "provider_evidence_ref",
            "error_code", "error_detail",
            "entitlement_version", "lease_sequence",
            "prepared_at", "dispatching_at", "completed_at", "updated_at",
        }
        assert required.issubset(columns), f"Missing: {required - columns}"

    def test_dispatch_state_check_constraint(self):
        names = _check_constraint_names(ProviderCallAttemptRecord)
        assert "dispatch_state_valid" in names


# ---------------------------------------------------------------------------
# ProviderCostLedgerEntryRecord required columns (spec section 2.11)
# ---------------------------------------------------------------------------

class TestProviderCostLedgerEntryRecordColumns:
    def test_required_columns(self):
        columns = _column_names(ProviderCostLedgerEntryRecord)
        required = {
            "id", "workspace_id", "operation_id", "job_id",
            "spend_reservation_id", "credential_version_id",
            "entry_type", "direction", "execution_mode",
            "semantic_key_hash",
            "provider", "model",
            "provider_amount", "provider_currency",
            "budget_amount", "budget_currency",
            "exchange_rate", "exchange_rate_source",
            "exchange_rate_observed_at", "price_rule_version",
            "related_entry_id", "adjusts_entry_id",
            "provider_evidence_ref", "reconciliation_evidence_ref",
            "audit_event_id", "audit_actor_id",
            "simulated_amount", "simulated_currency",
            "note", "created_at",
        }
        assert required.issubset(columns), f"Missing: {required - columns}"

    def test_entry_type_check_constraint(self):
        names = _check_constraint_names(ProviderCostLedgerEntryRecord)
        assert "provider_cost_ledger_entry_type_valid" in names

    def test_direction_check_constraint(self):
        names = _check_constraint_names(ProviderCostLedgerEntryRecord)
        assert "provider_cost_ledger_direction_valid" in names

    def test_execution_mode_check_constraint(self):
        names = _check_constraint_names(ProviderCostLedgerEntryRecord)
        assert "execution_mode_valid" in names


# ---------------------------------------------------------------------------
# UsageEventRecord required columns (spec section 2.12)
# ---------------------------------------------------------------------------

class TestUsageEventRecordColumns:
    def test_required_columns(self):
        columns = _column_names(UsageEventRecord)
        required = {
            "id", "workspace_id",
            "metric_key", "measurement_kind", "direction",
            "quantity", "unit", "execution_mode",
            "source_resource_type", "source_resource_id",
            "semantic_key_hash",
            "occurred_at", "created_at",
        }
        assert required.issubset(columns), f"Missing: {required - columns}"


# ---------------------------------------------------------------------------
# CustomerNotificationOutboxRecord required columns (spec section 2.13)
# ---------------------------------------------------------------------------

class TestCustomerNotificationOutboxRecordColumns:
    def test_required_columns(self):
        columns = _column_names(CustomerNotificationOutboxRecord)
        required = {
            "id", "workspace_id",
            "notification_type", "dimension", "threshold",
            "guardrail_revision", "platform_policy_version",
            "window_kind", "window_key",
            "period_start", "period_end",
            "crossing_sequence", "deduplication_key",
            "customer_payload_json",
            "delivery_state", "attempts",
            "next_attempt_at", "delivery_lease_id",
            "delivery_lease_expires_at",
            "delivered_at", "created_at",
        }
        assert required.issubset(columns), f"Missing: {required - columns}"

    def test_delivery_state_check_constraint(self):
        names = _check_constraint_names(CustomerNotificationOutboxRecord)
        assert "delivery_state_valid" in names


# ---------------------------------------------------------------------------
# GuardrailNotificationStateRecord required columns (spec section 2.13)
# ---------------------------------------------------------------------------

class TestGuardrailNotificationStateRecordColumns:
    def test_required_columns(self):
        columns = _column_names(GuardrailNotificationStateRecord)
        required = {
            "id", "workspace_id",
            "dimension", "threshold",
            "guardrail_revision", "platform_policy_version",
            "window_kind", "window_key",
            "period_start", "period_end",
            "state", "crossing_sequence",
            "updated_at",
        }
        assert required.issubset(columns), f"Missing: {required - columns}"

    def test_state_check_constraint(self):
        names = _check_constraint_names(GuardrailNotificationStateRecord)
        assert "state_valid" in names

    def test_crossing_sequence_check_constraint(self):
        names = _check_constraint_names(GuardrailNotificationStateRecord)
        assert "crossing_seq_nonnegative" in names


# ---------------------------------------------------------------------------
# EntitlementSnapshotInboxRecord CHECK constraints
# ---------------------------------------------------------------------------

class TestEntitlementSnapshotInboxCheckConstraints:
    def test_outcome_check_constraint(self):
        names = _check_constraint_names(EntitlementSnapshotInboxRecord)
        assert "entitlement_inbox_outcome_valid" in names

    def test_delivery_kind_check_constraint(self):
        names = _check_constraint_names(EntitlementSnapshotInboxRecord)
        assert "delivery_kind_valid" in names
