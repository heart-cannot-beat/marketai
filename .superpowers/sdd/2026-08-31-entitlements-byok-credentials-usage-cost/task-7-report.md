# Task 7 Report: Domain SQLAlchemy Models -- Fix Critical Review Findings

## Status: DONE

## Changes

### CredentialVersionRecord (credentials.py)
- Added `provider` (String(64)) column
- Added `state` column with CHECK constraint for active/rotated/revoked
- Added `display_label` column
- Replaced `encrypted_value` with `encrypted_secret` (spec naming)
- Added `encrypted_data_key` (LargeBinary) for envelope encryption
- Replaced `aad_context`/`value_sha256` with `aad_schema_version` (Integer)
- Added `rotated_at`/`rotated_by` columns
- Added CHECK constraint `version_no >= 1`
- Updated partial unique index to use `state = 'active'` instead of `revoked_at IS NULL`

### CredentialBindingRecord (credentials.py)
- Replaced `provider_key` with `provider` (spec naming)
- Added `binding_scope` column
- Added `revision` column for ETag concurrency
- Replaced `bound_at`/`unbound_at`/`unbound_by` with `created_at`/`updated_at` (spec uses projection model)
- Changed UNIQUE constraint to `(workspace_id, binding_scope, provider)`

### SpendGuardrailRecord (cost_models.py)
- Renamed columns to match spec: `revision`, `per_operation_limit`, `rolling_24h_limit`, `period_limit`, `outstanding_exposure_limit`, `updated_at`, `updated_by`
- Removed old column names: `guardrail_revision`, `max_cost_per_operation`, `max_cost_per_day`, `max_cost_per_billing_period`, `daily_exposure_soft_cap`, `billing_period_exposure_soft_cap`, `admin_updated_at`, `admin_updated_by`

### SpendGuardrailEventRecord (cost_models.py)
- Added `revision_before`/`revision_after` (replaced single `guardrail_revision`)
- Added `old_budget_currency`/`new_budget_currency`
- Added `reason` column
- Added `audit_event_id`/`mfa_evidence_ref`
- Replaced `event_type` with spec-aligned fields

### SpendReservationRecord (cost_models.py)
- Added `UNIQUE(workspace_id, id)` candidate key
- Added `execution_mode` column
- Replaced `provider_key`/`call_index`/`estimated_amount`/`currency`/`settled_amount` with spec fields: `provider`, `provider_currency`, `maximum_provider_amount`, `budget_currency`, `maximum_budget_amount`, `outstanding_budget_amount`
- Added `period_start`/`period_end`/`entitlement_version`/`lease_sequence`/`guardrail_revision`/`platform_policy_version`
- Added `updated_at`
- Added CHECK constraints: state valid, amounts non-negative, outstanding <= maximum

### CapacityReservationRecord (cost_models.py)
- Added `UNIQUE(workspace_id, id)` candidate key
- Replaced `provider_key`/`resource_type` with `entitlement_key`/`unit`
- Added `entitlement_version`/`lease_sequence`
- Added `expires_at` (critical for crash recovery reconciler)
- Added `updated_at`/`resolved_at`
- Changed state values from held/released to reserved/committed/released/expired
- Added CHECK constraint on state values

### ProviderCallAttemptRecord (cost_models.py)
- Added `UNIQUE(workspace_id, id)` candidate key
- Added `credential_version_id` with composite FK to credential_versions
- Added `execution_mode`/`model`/`request_fingerprint_hash`/`provider_idempotency_key`/`provider_request_id`/`provider_evidence_ref`
- Replaced `provider_key` with `provider`
- Added `entitlement_version`/`lease_sequence`
- Replaced `started_at`/`finished_at` with `prepared_at`/`dispatching_at`/`completed_at`/`updated_at`
- Removed `request_payload_hash`/`response_status`/`response_payload_hash`/`dispatch_certainty`
- Added CHECK constraint on `dispatch_state`

### ProviderCostLedgerEntryRecord (cost_models.py)
- Added `UNIQUE(workspace_id, id)` candidate key
- Renamed `entry_kind` to `entry_type` with CHECK constraint
- Added `direction`/`execution_mode` with CHECK constraints
- Added `spend_reservation_id`/`credential_version_id` with composite FKs
- Split `amount`/`currency` into `provider_amount`/`provider_currency` + `budget_amount`/`budget_currency`
- Added `exchange_rate`/`exchange_rate_source`/`exchange_rate_observed_at`/`price_rule_version`
- Added `model`/`related_entry_id`/`adjusts_entry_id`/`provider_evidence_ref`/`reconciliation_evidence_ref`/`audit_event_id`/`audit_actor_id`
- Added `simulated_amount`/`simulated_currency`/`note`
- Replaced `call_attempt_id`/`provider_key`/`evidence_json` with spec-aligned fields

### UsageEventRecord (usage_models.py)
- Added `metric_key`/`measurement_kind`/`direction`/`unit`
- Added `execution_mode`
- Added `source_resource_type`/`source_resource_id`
- Added `occurred_at` (distinct from created_at)
- Removed `operation_id`/`job_id` FKs (spec does not include them)
- Removed `event_type`/`resource_type`/`metadata_json`
- Changed `quantity` type to Numeric(24, 12) to match financial precision

### CustomerNotificationOutboxRecord (usage_models.py)
- Added `UNIQUE(workspace_id, id)` candidate key
- Added spec fields: `dimension`, `threshold`, `guardrail_revision`, `platform_policy_version`, `window_kind`, `window_key`, `period_start`, `period_end`, `crossing_sequence`, `customer_payload_json`
- Added delivery lease fields: `attempts`, `next_attempt_at`, `delivery_lease_id`, `delivery_lease_expires_at`
- Added CHECK constraint on `delivery_state`
- Removed `channel`/`payload_json` (replaced by spec fields)

### GuardrailNotificationStateRecord (usage_models.py)
- Added `UNIQUE(workspace_id, id)` candidate key
- Changed state values from armed/fired/suppressed to below/notified (per spec)
- Added `window_kind`/`period_start`/`period_end`/`crossing_sequence`
- Added CHECK constraint `crossing_sequence >= 0`
- Replaced `fired_at`/`created_at` with `updated_at`

### EntitlementSnapshotInboxRecord (models.py)
- Added CHECK constraint on `outcome` (applied/duplicate/stale/version_jump_paused/conflict)
- Added CHECK constraint on `delivery_kind` (push/full_repair)

## Test Evidence
- 49 tests passed (4 in test_entitlement_models.py, 45 in test_cost_usage_models.py)
- Tests verify all spec-required columns and CHECK constraint names
