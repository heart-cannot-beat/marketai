# Issue #23 — Complete Design: Make Meta Read and PAUSED Launch Tenant-Safe and Sandbox-First

> **Generated**: 2026-09-07
> **Status**: Design candidate — not yet approved for implementation.
> **Issue**: [#23](https://github.com/heart-cannot-beat/marketai-archive/issues/23)
> **Blocked by**: #17 (closed), #22 (closed)
> **Scope**: Replace process-global Meta account selection with explicit Workspace MetaConnection/DataSource bindings and CredentialVersion call evidence; preserve three evidence modes (customer_sandbox, controlled_mock, internal_validation); version observation contracts with paginated raw evidence, cursor recovery, status history and reconciliation; scope schedules and 14-day observation clock to account, contract and Workspace; extend Launch from single-entry to frozen Experiment candidate set using equal-budget PAUSED canaries under one shared Campaign via FakeMetaAdapter only; keep write credentials isolated; expose Operation recovery/reconcile and append-only dual-actor audit through the BFF contract.

---

# 1. Problem Statement

The current implementation has five structural deficiencies this design addresses:

1. **Global Meta account selection.** Meta credentials are environment variables (`META_AD_ACCOUNT_ID`, `META_ACCESS_TOKEN`, `META_APP_SECRET`). `require_meta_credential_workspace()` checks `META_CREDENTIAL_WORKSPACE_ID` to restrict usage to one workspace, but the credential itself is process-global. There are no `CredentialVersion` records, no per-workspace bindings, and no call evidence linking a specific credential version to a specific API call.

2. **No tenant isolation for Meta evidence.** Meta sync runs, pages, status history, and ad bindings lack composite workspace+mode foreign keys. There is no RLS enforcement on Meta-domain tables. Evidence from different workspaces or modes could theoretically be mixed.

3. **Single-entry Launch.** The current Launch implementation (`app/launch/service.py`) provisions exactly one campaign, one ad set, and one ad. There is no candidate manifest, no multi-candidate fan-out, no budget parity enforcement, and no set-level activation gate.

4. **No evidence modes.** The system has no formal distinction between customer sandbox (no-network), controlled mock (no-network + fault injection), and internal_validation (real HTTP, read-only). CONTEXT.md (line 267-268) defines Meta Sandbox as no-network controlled Mock, with internal real-only-read accounts not belonging to customer Workspaces — a definition this design respects.

5. **No observation contracts.** Sync runs are not versioned against immutable contract generations. There is no cursor recovery, no independent observation clock, no coverage ledger, and no mechanism to prevent evidence from different contract configurations from being mixed.

---

# 2. Three Evidence Modes

Mode is immutable provenance — not a maturity ladder. Evidence cannot be "promoted" by changing its label; it must be re-observed and evaluated in the destination mode.

## 2.1 customer_sandbox

- **Network**: None. The FakeMetaAdapter services all calls.
- **Credential**: Binding carries NULL credential version refs — no KMS dereference. The credential material is never accessed for network purposes.
- **Adapter**: FakeMetaAdapter with a durable database-backed object store (`FakeMetaObjectRecord`). Returns curated scenario data from `ScenarioVersionRecord`.
- **Scope**: Any customer Workspace. The default mode for workspaces without real Meta credentials.
- **Evidence eligibility**: Persisted with full domain paths (sync runs, pages, status history, verdicts). Eligible for Scale and Experiment decisions within sandbox context.
- **Scenario**: `scenario_version_id` required on every call intent.

## 2.2 controlled_mock

- **Network**: None. The FakeMetaAdapter services all calls with fault injection capabilities.
- **Credential**: Same as customer_sandbox — binding carries NULL credential version refs.
- **Adapter**: FakeMetaAdapter configured with the scenario version's fault injection rules (latency, partial failure, rate limiting, inconsistent state).
- **Scope**: Any customer Workspace. Used for integration testing and fault-tolerance validation.
- **Evidence eligibility**: Persisted with full domain paths. Full domain/task/audit/persistence path executed. Excluded from decision eligibility — controlled_mock evidence never contributes to Scale or Experiment decisions. This is not ephemeral; the evidence is durable and queryable.
- **Scenario**: `scenario_version_id` required on every call intent.

## 2.3 internal_validation

- **Network**: Real HTTP calls to Meta API, read-only operations only.
- **Credential**: Binding carries real credential version refs (access_token_version_id and app_secret_version_id both NOT NULL). Material decrypted from CredentialVersionRecord via KMS envelope at call time.
- **Adapter**: HttpMetaReadAdapter (real HTTP, read endpoints only).
- **Scope**: Platform Workspace only (`is_platform_workspace = true`). Internal validation does not belong to any customer Workspace.
- **Evidence eligibility**: Persisted. Used for platform-internal validation of contract configurations, API behavior verification, and integration health checks. Not eligible for customer-facing Scale or Experiment decisions.
- **Scenario**: `scenario_version_id` must be NULL (real HTTP calls have no scenario).

## 2.4 Mode Isolation Invariant

Every evidence row stores `workspace_id` and `meta_mode`. RLS policies enforce isolation using `marketai_meta_context_is_current(candidate_workspace, candidate_mode)` — a SECURITY DEFINER function following the same pattern as the existing `marketai_context_is_current()` from the `e9f0a1b2c3d4` migration. The function reads the HMAC secret from the `tenant_context_secrets` table (inaccessible to API/worker roles), recomputes the signature server-side, and compares against the session's `marketai.context_signature` setting. Raw `current_setting()` is never used in policy expressions.

The HMAC payload extends the existing tenant context signature by appending `meta_mode`:

```
marketai-tenant-context-v1\n{workspace_id}\n{authorization_epoch}\n{meta_mode}
```

`meta_mode` is added to `ALL_SETTINGS` in `app/platform/tenancy.py` as `marketai.meta_mode` and written alongside the other settings by `apply_to_session()`. The Python-side `tenant_context_signature()` function is extended to accept and include `meta_mode` in the HMAC computation. Non-meta tables continue using the existing `marketai_context_is_current(workspace_id)` function unmodified.

Workers call `apply_to_session()` with a context that includes `meta_mode` and fail closed when any required setting is missing. Application roles cannot bypass RLS; `FORCE ROW LEVEL SECURITY` applies. Cross-mode admin/reconciliation uses a separate, explicitly privileged and audited path.

---

# 3. Schema

All new tables use `workspace_id` and `meta_mode` as part of their composite key structure. RLS policies enforce `(workspace_id, meta_mode)` isolation on every meta-domain table via the HMAC-signed context function (§2.4).

## 3.1 MetaConnectionRecord

The workspace-scoped binding that replaces process-global Meta account selection. One active connection per `(workspace_id, meta_mode)`.

```
MetaConnectionRecord:
  id: text PK, prefixed "mcon_"
  workspace_id: text NOT NULL, FK → workspace_records
  meta_mode: enum('customer_sandbox', 'controlled_mock', 'internal_validation') NOT NULL
  ad_account_id: text NOT NULL
  state: enum('active', 'retiring', 'retired') NOT NULL DEFAULT 'active'
  created_at: timestamptz NOT NULL
  updated_at: timestamptz NOT NULL
  retired_at: timestamptz | NULL
  
  UNIQUE(workspace_id, meta_mode) WHERE state = 'active'
    -- At most one active connection per workspace+mode
  UNIQUE(workspace_id, meta_mode, id)
    -- Composite key target for child FKs
  
  CHECK: (state = 'retired') = (retired_at IS NOT NULL)
  CHECK: meta_mode = 'internal_validation' 
         → workspace_id IN (SELECT id FROM workspace_records WHERE is_platform_workspace = true)
    -- Enforced by deferred constraint trigger, not CHECK (cross-table)
```

State machine:
```
active → retiring   (retirement requested, non-terminal provisionings exist)
active → retired    (retirement requested, no non-terminal provisionings)
retiring → retired  (all provisionings terminal)
```
No backward transitions. `retired` is terminal.

## 3.2 MetaDataSourceBindingRecord

Links a connection to an ad account with dual credential version references. Multiple bindings per connection are supported (different ad accounts or credential rotations).

```
MetaDataSourceBindingRecord:
  id: text PK, prefixed "mbind_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  meta_connection_id: text NOT NULL
  ad_account_id: text NOT NULL
  access_token_version_id: text | NULL
    -- FK to CredentialVersionRecord where credential_key = 'meta_access_token'
    -- NOT NULL for internal_validation; NULL for customer_sandbox and controlled_mock
  app_secret_version_id: text | NULL
    -- FK to CredentialVersionRecord where credential_key = 'meta_app_secret'
    -- NOT NULL for internal_validation; NULL for customer_sandbox and controlled_mock
  state: enum('active', 'superseded', 'supersession_required') NOT NULL DEFAULT 'active'
  superseded_by_id: text | NULL, FK → MetaDataSourceBindingRecord
  superseded_at: timestamptz | NULL
  created_at: timestamptz NOT NULL
  
  Composite FK: (workspace_id, meta_mode, meta_connection_id) 
    → MetaConnectionRecord (workspace_id, meta_mode, id)
  Composite FK: (workspace_id, access_token_version_id) 
    → credential_versions (workspace_id, id)
  Composite FK: (workspace_id, app_secret_version_id) 
    → credential_versions (workspace_id, id)
  
  UNIQUE(workspace_id, meta_mode, meta_connection_id, ad_account_id) WHERE state = 'active'
    -- One active binding per connection+account
  UNIQUE(workspace_id, meta_mode, id)
    -- Composite key target for child FKs
    
  CHECK: (state = 'superseded') = (superseded_by_id IS NOT NULL)
  CHECK: (state = 'superseded') = (superseded_at IS NOT NULL)
  CHECK: meta_mode = 'internal_validation' → access_token_version_id IS NOT NULL
  CHECK: meta_mode IN ('customer_sandbox', 'controlled_mock') 
         → access_token_version_id IS NULL AND app_secret_version_id IS NULL
```

State machine:
```
active → superseded              (explicit supersede operation)
active → supersession_required   (connection retirement initiated)
supersession_required → superseded (explicit supersede operation)
```

Supersession: a binding cannot be deactivated without a replacement. `POST .../bindings/:bid/supersede` atomically creates the new binding and supersedes the old one. `supersession_required` is entered during connection retirement (§8.7) to signal that the binding must be replaced before the connection can complete retirement.

## 3.3 CredentialVersionRecord (existing — `app/entitlements/credentials.py`)

This design references the existing `CredentialVersionRecord` and `CredentialBindingRecord` from the BYOK credential infrastructure. No schema changes to these tables.

Key existing properties used by #23:

- **KMS envelope encryption**: `encrypted_secret`, `encrypted_data_key`, `encryption_key_id`, `encryption_algorithm`, `nonce`, `aad_schema_version` — credential material is AES-256-GCM encrypted with workspace-bound AAD context.
- **Three-state lifecycle**: `active` → `rotated` → `revoked`. Revocation is terminal and append-only.
- **Composite unique**: `(workspace_id, id)` — target for composite FKs from MetaDataSourceBindingRecord and MetaCallIntentRecord.
- **Versioning unique**: `(workspace_id, credential_key, version_no)` — ensures monotonic version numbering per credential key per workspace.
- **Partial unique index**: at most one `active` credential per `(workspace_id, credential_key)`.
- **credential_key values for Meta**: `'meta_access_token'` for access tokens, `'meta_app_secret'` for app secrets.

#23 bootstrap provisioning creates `CredentialVersionRecord` rows with environment-backed material, encrypted via the existing KMS envelope. Full per-workspace token onboarding, rotation, and KMS lifecycle is deferred to #9.

## 3.4 ScenarioVersionRecord

Repeatable conditions for the FakeMetaAdapter in customer_sandbox and controlled_mock modes.

```
ScenarioVersionRecord:
  id: text PK, prefixed "scen_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  meta_connection_id: text NOT NULL
  scenario_config: jsonb NOT NULL  -- curated data sets, fault rules, timing
  scenario_hash: text NOT NULL  -- SHA-256 of canonical scenario_config
  is_default: boolean NOT NULL DEFAULT false
  created_at: timestamptz NOT NULL
  
  Composite FK: (workspace_id, meta_mode, meta_connection_id)
    → MetaConnectionRecord (workspace_id, meta_mode, id)
  UNIQUE(workspace_id, meta_mode, id)
    -- 3-column composite key target for child FKs
  UNIQUE(workspace_id, meta_mode, meta_connection_id, id)
    -- 4-column composite FK target: intent → scenario via same connection
  UNIQUE(workspace_id, meta_mode, meta_connection_id) WHERE is_default = true
    -- At most one default scenario per connection
    
  CHECK: meta_mode IN ('customer_sandbox', 'controlled_mock')
    -- internal_validation has no scenarios
```

## 3.5 MetaCallIntentRecord

The immutable record committed *before* a Meta API call. The intent commit is the linearization point — it establishes authorization by acquiring FOR SHARE locks on the connection and binding.

```
MetaCallIntentRecord:
  id: text PK, prefixed "mci_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  meta_connection_id: text NOT NULL
  meta_binding_id: text NOT NULL
  ad_account_id: text NOT NULL
  
  -- Dual credential version snapshot (frozen at intent time)
  access_token_version_id: text | NULL
    -- NOT NULL for internal_validation; NULL for sandbox/mock
  app_secret_version_id: text | NULL
    -- NOT NULL for internal_validation HMAC signing; NULL for sandbox/mock
  
  operation_id: text NOT NULL, FK → operation_records
  
  -- Request identity
  meta_api_method: text NOT NULL  -- e.g., 'GET', 'POST'
  meta_api_endpoint: text NOT NULL  -- e.g., '/v21.0/{ad_account_id}/insights'
  request_parameters: jsonb NOT NULL
  request_fingerprint_hash: text NOT NULL
    -- SHA-256 of canonical Meta API request identity
  provider_idempotency_key: text | NULL
    -- HMAC-SHA256(connection_secret, canonical_request_identity)
    -- Sent to Meta for their dedup; NULL for read-only calls
    
  -- Authorization snapshot (immutable at commit time)
  connection_state_at_intent: text NOT NULL  -- 'active'
  binding_state_at_intent: text NOT NULL  -- 'active'
  
  -- Dispatch marker
  call_type: enum('saga_step', 'page_fetch') NOT NULL
  network_dispatched_at: timestamptz | NULL
    -- Set to now() just before the adapter call is made.
    -- If non-NULL and no outcome exists, the intent is uncertain.
  
  -- Mode-specific
  scenario_version_id: text | NULL
  write_permitted: boolean NOT NULL
  
  executor: text NOT NULL  -- canonical executor (§8.6)
  created_at: timestamptz NOT NULL
  
  Composite FK: (workspace_id, meta_mode, meta_connection_id)
    → MetaConnectionRecord
  Composite FK: (workspace_id, meta_mode, meta_binding_id)
    → MetaDataSourceBindingRecord
  Composite FK: (workspace_id, meta_mode, meta_connection_id, scenario_version_id)
    → ScenarioVersionRecord (when scenario_version_id IS NOT NULL)
  Composite FK: (workspace_id, access_token_version_id)
    → credential_versions (workspace_id, id)
  Composite FK: (workspace_id, app_secret_version_id)
    → credential_versions (workspace_id, id)
  
  CHECK: (meta_mode = 'internal_validation' AND scenario_version_id IS NULL)
      OR (meta_mode != 'internal_validation' AND scenario_version_id IS NOT NULL)
  CHECK: (meta_mode = 'internal_validation') = (access_token_version_id IS NOT NULL)
  CHECK: write_permitted = false  -- #23 scope: read and PAUSED only
```

The intent does not carry attempt FKs — attempts reference intents (§3.11, §3.19), not the reverse. This eliminates the cyclic FK dependency. The `network_dispatched_at` field serves as the dispatch marker for uncertainty detection: an intent with `network_dispatched_at IS NOT NULL` and no corresponding outcome is uncertain.

## 3.6 MetaCallOutcomeRecord

Appended *after* a Meta API call completes (or fails to complete). Immutable.

```
MetaCallOutcomeRecord:
  id: text PK, prefixed "mco_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  intent_id: text NOT NULL, FK → MetaCallIntentRecord
  operation_id: text NOT NULL, FK → operation_records
  
  -- Result
  network_attempted: boolean NOT NULL
  http_status: integer | NULL
  response_evidence_hash: text | NULL  -- SHA-256 of response body
  response_summary: jsonb | NULL  -- structured extraction of key response fields
  error_category: text | NULL  -- e.g., 'rate_limit', 'auth_failure', 'timeout'
  error_detail: text | NULL
  
  -- Recovery cross-reference
  inspected_operation_id: text | NULL, FK → operation_records
    -- Set when this outcome is from a recovery operation inspecting another
  
  executor: text NOT NULL
  created_at: timestamptz NOT NULL
  
  CHECK: network_attempted = false → http_status IS NULL
  CHECK: (inspected_operation_id IS NULL) OR (inspected_operation_id != operation_id)
```

## 3.7 MetaCallReconciliationRecord

Supersedes uncertain intents with new inspection attempts. Does not fabricate outcomes.

```
MetaCallReconciliationRecord:
  id: text PK, prefixed "mcrec_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  uncertain_intent_id: text NOT NULL, FK → MetaCallIntentRecord
  inspection_intent_id: text NOT NULL, FK → MetaCallIntentRecord
  inspection_outcome_id: text NOT NULL, FK → MetaCallOutcomeRecord
  resolution: enum('confirmed_succeeded', 'confirmed_failed', 'inconclusive') NOT NULL
  created_at: timestamptz NOT NULL
  
  UNIQUE(uncertain_intent_id, inspection_intent_id)
```

## 3.8 ObservationContractGenerationRecord

Immutable contract generation. A new generation is created whenever account, fields, attribution, breakdown, window, or delivery configuration changes. Evidence references the exact generation.

```
ObservationContractGenerationRecord:
  id: text PK, prefixed "ocg_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  meta_connection_id: text NOT NULL
  contract_type: text NOT NULL  -- e.g., 'daily_insights', 'delivery_config'
  generation_number: integer NOT NULL  -- monotonically increasing per scope
  
  -- Contract definition (immutable)
  ad_account_id: text NOT NULL
  fields: jsonb NOT NULL  -- sorted field list
  attribution_window: jsonb NOT NULL  -- e.g., {"click": "7d", "view": "1d"}
  breakdowns: jsonb NOT NULL  -- sorted breakdown list
  time_window: jsonb NOT NULL  -- e.g., {"granularity": "daily"}
  delivery_config: jsonb | NULL  -- for delivery-config contracts
  contract_hash: text NOT NULL  -- SHA-256 of canonical contract definition
  local_timezone: text NOT NULL
    -- e.g., 'America/Los_Angeles'. Frozen at generation creation.
    -- All date arithmetic (coverage_date, 14-day clock) uses this timezone.
    -- Cannot be changed after creation.
  
  -- Lifecycle
  state: enum('active', 'retired') NOT NULL DEFAULT 'active'
  clock_started_at: timestamptz | NULL
    -- Set after the generation's first complete, valid collection
    -- NOT when the row is created
  retired_at: timestamptz | NULL
  retired_by_generation_id: text | NULL, FK → self
  
  created_at: timestamptz NOT NULL
  
  Composite FK: (workspace_id, meta_mode, meta_connection_id)
    → MetaConnectionRecord
  UNIQUE(workspace_id, meta_mode, meta_connection_id, contract_type)
    WHERE state = 'active'
    -- One active generation per scope
  UNIQUE(workspace_id, meta_mode, meta_connection_id, contract_type, generation_number)
  UNIQUE(workspace_id, meta_mode, id)
    -- Composite key target for child FKs
    
  CHECK: (state = 'retired') = (retired_at IS NOT NULL)
```

First-generation serialization: when no active generation exists, a transaction-scoped advisory lock is acquired before `SELECT ... FOR UPDATE` to prevent a race on nonexistent rows.

## 3.9 MetaSyncRunRecord

One sync run per occurrence per contract generation. Occurrence identity prevents duplicate runs for the same trigger event.

```
MetaSyncRunRecord:
  id: text PK, prefixed "msrun_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  contract_generation_id: text NOT NULL
  occurrence_id: text NOT NULL  -- deterministic identifier for the trigger event
  run_generation: integer NOT NULL DEFAULT 1
    -- Incremented when a superseded run is replaced
  
  -- Execution state
  state: enum('queued', 'running', 'succeeded', 'failed', 'superseded') NOT NULL
  operation_id: text NOT NULL, FK → operation_records
  
  -- Cursor recovery
  last_completed_page_order: integer | NULL
  output_cursor: text | NULL  -- for resuming pagination
  
  -- Evidence
  total_pages: integer | NULL
  total_rows: integer | NULL
  
  executor: text NOT NULL
  created_at: timestamptz NOT NULL
  completed_at: timestamptz | NULL
  
  Composite FK: (workspace_id, meta_mode, contract_generation_id)
    → ObservationContractGenerationRecord (workspace_id, meta_mode, id)
    
  UNIQUE(occurrence_id, run_generation)
  
  CHECK: state IN ('succeeded') → completed_at IS NOT NULL
```

Occurrence CTE safety: use `INSERT ... ON CONFLICT DO NOTHING` then a separate `SELECT` for the existing row. Do not use `DO UPDATE SET id = EXCLUDED.id` which would change the existing PK.

## 3.10 MetaEvidencePageRecord

Individual pages of paginated Meta API responses. Immutable once written.

```
MetaEvidencePageRecord:
  id: text PK, prefixed "mpage_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  sync_run_id: text NOT NULL, FK → MetaSyncRunRecord
  contract_generation_id: text NOT NULL
  
  page_order: integer NOT NULL  -- 1-based sequential
  input_cursor: text | NULL  -- cursor used to request this page (NULL for first)
  output_cursor: text | NULL  -- cursor for requesting the next page (NULL for last)
  
  -- Raw evidence
  payload_checksum: text NOT NULL  -- SHA-256 of raw compressed payload
  payload_compressed: bytea NOT NULL  -- gzip of raw JSON response
  row_count: integer NOT NULL
  
  -- Completion
  is_terminal: boolean NOT NULL DEFAULT false
    -- true when this is the last page (output_cursor IS NULL or empty)
  
  intent_id: text NOT NULL, FK → MetaCallIntentRecord
  outcome_id: text NOT NULL, FK → MetaCallOutcomeRecord
  
  created_at: timestamptz NOT NULL
  
  Composite FK: (workspace_id, meta_mode, contract_generation_id)
    → ObservationContractGenerationRecord
  UNIQUE(sync_run_id, page_order)
  
  CHECK: is_terminal = true → output_cursor IS NULL
  CHECK: page_order >= 1
```

Terminal page atomicity: the final page and the sync run's `succeeded` state transition are written in the same transaction.

## 3.11 MetaPageFetchAttemptRecord

Per-page fetch attempt tracking for crash recovery and pagination resumption.

```
MetaPageFetchAttemptRecord:
  id: text PK, prefixed "mpfa_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  sync_run_id: text NOT NULL, FK → MetaSyncRunRecord
  intent_id: text NOT NULL, FK → MetaCallIntentRecord
  page_order: integer NOT NULL
  attempt_number: integer NOT NULL  -- monotonically increasing per (run, page)
  
  state: enum('started', 'succeeded', 'failed') NOT NULL
  input_cursor: text | NULL
  error_detail: text | NULL
  
  created_at: timestamptz NOT NULL
  completed_at: timestamptz | NULL
  
  UNIQUE(sync_run_id, page_order, attempt_number)
```

## 3.12 MetaStatusHistoryRecord

Append-only status change observations from Meta API.

```
MetaStatusHistoryRecord:
  id: text PK, prefixed "mstat_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  contract_generation_id: text NOT NULL
  entity_type: text NOT NULL  -- 'campaign', 'adset', 'ad'
  entity_id: text NOT NULL  -- Meta's remote ID
  
  observed_status: text NOT NULL  -- Meta's effective_status value
  previous_status: text | NULL
  
  sync_run_id: text NOT NULL, FK → MetaSyncRunRecord
  evidence_page_id: text NOT NULL, FK → MetaEvidencePageRecord
  
  observed_at: timestamptz NOT NULL
  created_at: timestamptz NOT NULL
  
  Composite FK: (workspace_id, meta_mode, contract_generation_id)
    → ObservationContractGenerationRecord
```

## 3.13 ObservationCoverageLedgerRecord

Tracks which local dates have complete, valid observations for a contract generation. The 14-day observation clock requires 14 consecutive covered dates.

```
ObservationCoverageLedgerRecord:
  id: text PK
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  contract_generation_id: text NOT NULL
  
  coverage_date: date NOT NULL  -- the local date covered (in contract's local_timezone)
  sync_run_id: text NOT NULL, FK → MetaSyncRunRecord
    -- the run that established coverage for this date
  attestation_id: text NOT NULL, FK → EvidenceAttestationRecord
    -- the valid attestation backing this coverage entry
  
  created_at: timestamptz NOT NULL
  
  Composite FK: (workspace_id, meta_mode, contract_generation_id)
    → ObservationContractGenerationRecord
  UNIQUE(contract_generation_id, coverage_date)
    -- One coverage entry per date per generation
```

Tenure calculation: count the longest streak of consecutive `coverage_date` values. The observation clock starts (`clock_started_at` on the contract generation) only after the first complete, valid collection — not when the row is created. Tenure tracking is derived from the ledger entries, not stored as a mutable counter. Coverage entries are created only from valid (non-invalidated) attestations. If an attestation is later invalidated, its coverage entries are removed from the ledger and the coverage streak is recalculated.

## 3.14 MetaSyncScheduleRecord

Workspace-scoped schedule for periodic sync runs. Uses SKIP LOCKED for concurrent dispatch.

```
MetaSyncScheduleRecord:
  id: text PK, prefixed "msched_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  meta_connection_id: text NOT NULL
  contract_generation_id: text NOT NULL
  
  cron_expression: text NOT NULL
  state: enum('active', 'paused', 'retired') NOT NULL DEFAULT 'active'
  next_fire_at: timestamptz | NULL
  last_fired_at: timestamptz | NULL
  
  created_at: timestamptz NOT NULL
  
  Composite FK: (workspace_id, meta_mode, meta_connection_id)
    → MetaConnectionRecord
  Composite FK: (workspace_id, meta_mode, contract_generation_id)
    → ObservationContractGenerationRecord
```

Dispatcher query uses `SELECT ... FOR UPDATE SKIP LOCKED` to prevent concurrent dispatchers from claiming the same schedule.

## 3.15 LaunchPreviewRecord

Frozen preview of a multi-candidate launch. Created during preview creation, contains the complete candidate manifest. Confirmation is handled by the existing `LaunchConfirmationRecord` protocol (§5.2).

```
LaunchPreviewRecord:
  id: text PK, prefixed "lprev_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  meta_connection_id: text NOT NULL
  meta_binding_id: text NOT NULL
  
  experiment_id: text NOT NULL, FK → ExperimentRecord
  experiment_version_id: text NOT NULL, FK → ExperimentVersionRecord
  
  -- Frozen manifest
  candidate_count: integer NOT NULL
  campaign_config: jsonb NOT NULL  -- shared campaign parameters
  daily_budget_minor: bigint NOT NULL  -- per-adset, in minor currency units
  currency: text NOT NULL
  
  -- Snapshot integrity
  preview_hash: text NOT NULL  -- SHA-256 of canonical preview content
  frozen_snapshot: jsonb NOT NULL  -- complete serialized preview state
  
  -- State
  state: enum('draft', 'confirmed', 'expired') NOT NULL DEFAULT 'draft'
  confirmed_at: timestamptz | NULL
  expires_at: timestamptz NOT NULL
  
  created_at: timestamptz NOT NULL
  
  Composite FK: (workspace_id, meta_mode, meta_connection_id)
    → MetaConnectionRecord
  Composite FK: (workspace_id, meta_mode, meta_binding_id)
    → MetaDataSourceBindingRecord
    
  CHECK: (state = 'confirmed') = (confirmed_at IS NOT NULL)
  CHECK: daily_budget_minor > 0
  CHECK: candidate_count >= 1
```

Preview creation response includes: `preview_id`, `preview_hash`, `candidate_count`, `daily_budget_minor`, `currency`, `total_daily_cost_minor` (= `daily_budget_minor * candidate_count`), binding evidence (`connection_id`, `ad_account_id`, credential state snapshots at preview time), and `expires_at`. This gives the confirming user full cost and binding visibility before confirmation.

## 3.16 LaunchCandidateRecord

One per candidate in a frozen preview. Immutable after creation.

```
LaunchCandidateRecord:
  id: text PK, prefixed "lcand_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  preview_id: text NOT NULL, FK → LaunchPreviewRecord
  
  candidate_index: integer NOT NULL  -- 0-based position in manifest
  experiment_version_candidate_id: text NOT NULL
    -- FK → ExperimentVersionCandidateRecord
  
  -- Creative content snapshot
  asset_version_id: text NOT NULL
  creative_config: jsonb NOT NULL
  
  -- Deterministic idempotency keys (per-candidate scope)
  adset_idempotency_key: text NOT NULL
  ad_idempotency_key: text NOT NULL
  
  created_at: timestamptz NOT NULL
  
  UNIQUE(preview_id, candidate_index)
  UNIQUE(workspace_id, meta_mode, id)
    -- Composite key target for child FKs
  
  CHECK: candidate_index >= 0
```

Idempotency keys are deterministic: `HMAC-SHA256(connection_secret, canonical_identity)` where canonical identity includes connection ID, preview ID, candidate index, and object type. The campaign-level idempotency key is on `LaunchProvisioningRecord` (§3.17) since the campaign is shared across all candidates.

## 3.17 LaunchProvisioningRecord

Renamed from `LaunchOperationRecord` to avoid collision with the generic `OperationRecord`. Tracks the saga state for provisioning Meta objects.

```
LaunchProvisioningRecord:
  id: text PK, prefixed "lprov_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  meta_connection_id: text NOT NULL
  preview_id: text NOT NULL, FK → LaunchPreviewRecord
  confirmation_id: text NOT NULL, FK → launch_confirmations
  operation_id: text NOT NULL, FK → operation_records
  
  -- Campaign-level idempotency key (shared campaign)
  campaign_idempotency_key: text NOT NULL
    -- HMAC-SHA256(connection_secret, "campaign:{preview_id}")
  
  -- Provisioning state (separate from activation)
  provisioning_state: enum('pending', 'provisioning', 'provisioned', 
                           'partially_provisioned', 'failed') NOT NULL
  -- Activation state (only meaningful after provisioning completes)
  activation_state: enum('paused', 'activating', 'active', 'failed') NOT NULL DEFAULT 'paused'
  
  -- Remote IDs (populated as saga steps complete)
  remote_campaign_id: text | NULL
  
  -- Fencing
  fencing_token: bigint NOT NULL DEFAULT 0
    -- Monotonically increasing; stale-token writes rejected by CAS check
  
  created_at: timestamptz NOT NULL
  updated_at: timestamptz NOT NULL
  
  Composite FK: (workspace_id, meta_mode, meta_connection_id)
    → MetaConnectionRecord
    
  CHECK: activation_state != 'paused' → provisioning_state = 'provisioned'
```

In #23 scope, `activation_state` remains `'paused'` for all provisionings — activation is deferred (§9). The `activating`/`active` states are reserved for forward compatibility.

## 3.18 LaunchProvisioningStatusHistoryRecord

Append-only status changes for provisioning records.

```
LaunchProvisioningStatusHistoryRecord:
  id: text PK
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  provisioning_id: text NOT NULL, FK → LaunchProvisioningRecord
  
  field: enum('provisioning_state', 'activation_state') NOT NULL
  from_state: text NOT NULL
  to_state: text NOT NULL
  reason: text | NULL
  executor: text NOT NULL
  
  created_at: timestamptz NOT NULL
```

## 3.19 LaunchSagaStepAttemptRecord

Per-Meta-call persistence within the Launch saga. Each step attempt is a single Meta API interaction.

```
LaunchSagaStepAttemptRecord:
  id: text PK, prefixed "lssa_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  provisioning_id: text NOT NULL, FK → LaunchProvisioningRecord
  intent_id: text NOT NULL, FK → MetaCallIntentRecord
  candidate_id: text | NULL, FK → LaunchCandidateRecord
    -- NULL for campaign-level steps (create_campaign, readback_campaign)
    -- NOT NULL for candidate-level steps (create_adset, create_ad, etc.)
  
  step_type: text NOT NULL
    -- Campaign-level: 'create_campaign', 'readback_campaign'
    -- Candidate-level: 'create_adset', 'upload_image',
    --                   'create_creative', 'create_ad',
    --                   'readback_adset', 'readback_ad'
  attempt_number: integer NOT NULL
  
  -- Lease/fencing
  fencing_token: bigint NOT NULL
    -- Must match provisioning record's current fencing_token
  
  -- State
  state: enum('started', 'intent_committed', 'outcome_received', 'applied') NOT NULL
  
  -- Remote evidence (populated as available)
  remote_object_id: text | NULL  -- Meta's ID for the created/read object
  
  executor: text NOT NULL
  created_at: timestamptz NOT NULL
  completed_at: timestamptz | NULL
  
  -- Campaign-level steps: one attempt sequence per step_type
  UNIQUE(provisioning_id, step_type, attempt_number) WHERE candidate_id IS NULL
  -- Candidate-level steps: one attempt sequence per (step_type, candidate)
  UNIQUE(provisioning_id, step_type, candidate_id, attempt_number) WHERE candidate_id IS NOT NULL
```

The attempt carries `intent_id` — the attempt references the intent, not the reverse. Intent + attempt are inserted in one atomic authorization transaction (§4.2). A guard rejects new attempts when unapplied ones exist for the same (provisioning, candidate, step_type).

## 3.20 LaunchParityCheckRecord

Coherent set-level evaluation of the entire candidate set after provisioning or activation.

```
LaunchParityCheckRecord:
  id: text PK, prefixed "lpc_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  provisioning_id: text NOT NULL, FK → LaunchProvisioningRecord
  
  check_type: enum('post_provisioning', 'post_activation', 'periodic') NOT NULL
  
  -- Parity evaluation
  all_candidates_present: boolean NOT NULL
  budget_parity_verified: boolean NOT NULL
  config_parity_verified: boolean NOT NULL
  status_parity_verified: boolean NOT NULL
  
  -- Detail
  detail: jsonb NOT NULL  -- per-candidate readback evidence
  
  passed: boolean NOT NULL
  executor: text NOT NULL
  created_at: timestamptz NOT NULL
```

## 3.21 LaunchReconciliationRecord

Serializes reconciliation of uncertain Launch operations. One open reconciliation per original operation.

```
LaunchReconciliationRecord:
  id: text PK, prefixed "lrec_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  original_operation_id: text NOT NULL, FK → operation_records
  recovery_operation_id: text NOT NULL, FK → operation_records, UNIQUE
  
  state: enum('open', 'resolved') NOT NULL DEFAULT 'open'
  resolution: enum('succeeded', 'partially_succeeded', 'failed', 'cancelled') | NULL
  
  created_at: timestamptz NOT NULL
  resolved_at: timestamptz | NULL
  
  UNIQUE(original_operation_id) WHERE state = 'open'
    -- At most one open reconciliation per original operation
    
  CHECK: (state = 'resolved') = (resolution IS NOT NULL)
  CHECK: (state = 'resolved') = (resolved_at IS NOT NULL)
```

## 3.22 EvidencePublicationRecord

Publication of a sync run's evidence for use in decisions. Supports multiple publication generations (retries after terminal failure).

```
EvidencePublicationRecord:
  id: text PK, prefixed "epub_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  sync_run_id: text NOT NULL, FK → MetaSyncRunRecord
  publication_generation: integer NOT NULL
  
  state: enum('validating', 'publishing', 'published', 'failed') NOT NULL
  operation_id: text NOT NULL, FK → operation_records
    -- Uses create_or_replay_operation for recovery
  
  validation_hash: text | NULL  -- hash of validated evidence set
  published_at: timestamptz | NULL
  failed_at: timestamptz | NULL
  failure_reason: text | NULL
  
  created_at: timestamptz NOT NULL
  
  UNIQUE(sync_run_id, publication_generation)
  
  CHECK: (state = 'published') = (published_at IS NOT NULL)
  CHECK: publication_generation >= 1
```

Run-level serialization against invalidation: while a publication is in `validating` or `publishing` state, no new publication generation can start for the same sync run.

## 3.23 EvidenceAttestationRecord

Records that a published evidence run is eligible for decisions. Ranked by evidence watermark (factual date coverage), not by publication timestamp.

```
EvidenceAttestationRecord:
  id: text PK, prefixed "eatt_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  contract_generation_id: text NOT NULL
  publication_id: text NOT NULL, FK → EvidencePublicationRecord
  
  state: enum('valid', 'invalidated') NOT NULL DEFAULT 'valid'
  invalidated_at: timestamptz | NULL
  invalidation_reason: text | NULL
  
  evidence_watermark: daterange NOT NULL
    -- The date range (in contract's local_timezone) the attested evidence covers.
    -- e.g., [2026-09-01, 2026-09-07]. Immutable after creation.
    -- Ranking: upper(evidence_watermark) DESC, published_at DESC as tiebreaker.
  
  published_at: timestamptz NOT NULL
    -- Copied from publication for query efficiency; used as tiebreaker only
  created_at: timestamptz NOT NULL
  
  Composite FK: (workspace_id, meta_mode, contract_generation_id)
    → ObservationContractGenerationRecord
```

Attestation ranking: when querying the current evidence for decisions, select the attestation with `state = 'valid'` and `upper(evidence_watermark) DESC`, `published_at DESC` as tiebreaker. A restored historical run with an old watermark cannot become current if a valid attestation with a newer watermark exists — regardless of when either was published.

## 3.24 Typed Taint Tables

Taint propagation uses typed tables per domain, not generic polymorphic FKs. Invalidation resolution happens at run level.

```
ScaleVerdictTaintRecord:
  id: text PK
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  verdict_id: text NOT NULL  -- FK to scale verdict record
  taint_source_type: text NOT NULL  -- e.g., 'evidence_invalidation', 'contract_change'
  taint_source_id: text NOT NULL  -- FK to the specific invalidation record
  sync_run_id: text NOT NULL, FK → MetaSyncRunRecord
  contract_generation_id: text NOT NULL
  
  state: enum('active', 'resolved') NOT NULL DEFAULT 'active'
  resolved_at: timestamptz | NULL
  resolution: enum('evidence_restored', 'verdict_withdrawn') | NULL
  
  created_at: timestamptz NOT NULL
  
  Composite FK: (workspace_id, meta_mode, contract_generation_id)
    → ObservationContractGenerationRecord
  -- Lineage FK constraining run to contract:
  FK: (sync_run_id) → MetaSyncRunRecord
    with CHECK: MetaSyncRunRecord.contract_generation_id = ScaleVerdictTaintRecord.contract_generation_id
```

Similar typed taint tables exist for each domain that depends on evidence:
- `ExperimentJudgementTaintRecord` — taints on experiment judgements
- `LaunchDecisionTaintRecord` — taints on launch decisions

## 3.25 VerdictSupersessionRecord

Prevents restored historical decisions from becoming current.

```
VerdictSupersessionRecord:
  id: text PK
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  superseded_verdict_id: text NOT NULL
  superseding_verdict_id: text NOT NULL
  reason: text NOT NULL  -- e.g., 'newer_evidence', 'manual_withdrawal'
  
  created_at: timestamptz NOT NULL
  
  UNIQUE(superseded_verdict_id)
    -- A verdict can only be superseded once
```

When a new verdict is produced from newer evidence, the old verdict is superseded. Restored historical runs do not automatically resurrect superseded verdicts — the supersession record stands.

## 3.26 FakeMetaObjectRecord

Durable database-backed object store for the FakeMetaAdapter. Not in-memory.

```
FakeMetaObjectRecord:
  id: text PK, prefixed "fmo_"
  workspace_id: text NOT NULL
  meta_mode: enum NOT NULL
  scenario_version_id: text NOT NULL
  
  object_type: text NOT NULL  -- 'campaign', 'adset', 'ad', 'creative', 'image'
  remote_object_id: text NOT NULL  -- fake Meta ID assigned by the adapter
  
  state: jsonb NOT NULL  -- full object state including status
  created_at: timestamptz NOT NULL
  updated_at: timestamptz NOT NULL
  
  Composite FK: (workspace_id, meta_mode, scenario_version_id)
    → ScenarioVersionRecord (workspace_id, meta_mode, id)
  UNIQUE(workspace_id, meta_mode, scenario_version_id, remote_object_id)
```

Idempotency guarantee for the FakeMetaAdapter: write operations are idempotent within a scenario version. The same deterministic idempotency key produces the same fake object.

## 3.27 AuditEventRecord Extension

The existing `AuditEventRecord` (`app/adapters/persistence/models.py`) provides the append-only dual-actor audit trail. #23 extends it with one nullable column:

```
ALTER TABLE audit_events ADD COLUMN meta_mode text;
  -- Set for Meta-domain events; NULL for non-Meta events.
  -- RLS on audit_events uses the existing marketai_context_is_current(workspace_id)
  -- for workspace isolation. Meta-mode filtering is application-level, not RLS,
  -- since audit queries may legitimately span modes for a workspace.
```

Existing columns used by #23: `id`, `workspace_id`, `initiator_id`, `initiator_role`, `executor`, `request_id`, `trace_id`, `authorization_epoch`, `event_type`, `resource_type`, `resource_id`, `details_json`, `created_at`.

Meta-domain `event_type` values: `'meta.intent.created'`, `'meta.outcome.recorded'`, `'meta.connection.activated'`, `'meta.connection.retired'`, `'meta.binding.superseded'`, `'launch.provisioning.step_completed'`, `'launch.parity_check.completed'`, `'credential.revoked'`, `'evidence.published'`, `'evidence.invalidated'`.

The `details_json` field for Meta-domain events includes a `detail_hash` key (SHA-256 of the serialized detail) for tamper detection, plus structured references to intent/outcome/generation IDs.

## 3.28 RLS Policies

Every Meta-domain table has RLS policies enforced via the HMAC-signed context function:

```sql
ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <table> FORCE ROW LEVEL SECURITY;

CREATE POLICY <table>_tenant_isolation ON <table>
  USING (marketai_meta_context_is_current(workspace_id, meta_mode))
  WITH CHECK (marketai_meta_context_is_current(workspace_id, meta_mode));
```

The `marketai_meta_context_is_current(candidate_workspace text, candidate_mode text)` function is a SECURITY DEFINER SQL function following the same pattern as the existing `marketai_context_is_current()` from migration `e9f0a1b2c3d4`:

```sql
CREATE OR REPLACE FUNCTION marketai_meta_context_is_current(
    candidate_workspace text, candidate_mode text
)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT candidate_workspace = nullif(
               current_setting('marketai.workspace_id', true), ''
           )
       AND candidate_mode = nullif(
               current_setting('marketai.meta_mode', true), ''
           )
       AND EXISTS (
            SELECT 1
            FROM public.workspaces AS workspace
            CROSS JOIN public.tenant_context_secrets AS context_key
            WHERE context_key.key_id = 1
              AND workspace.id = candidate_workspace
              AND workspace.authorization_epoch = COALESCE(
                    NULLIF(
                        current_setting('marketai.authorization_epoch', true),
                        ''
                    )::integer,
                    -1
              )
              AND encode(
                    hmac(
                        convert_to(
                            'marketai-tenant-context-v1' || chr(10) ||
                            candidate_workspace || chr(10) ||
                            workspace.authorization_epoch::text || chr(10) ||
                            candidate_mode,
                            'UTF8'
                        ),
                        convert_to(context_key.secret, 'UTF8'),
                        'sha256'
                    ),
                    'hex'
              ) = COALESCE(
                    current_setting('marketai.context_signature', true),
                    ''
              )
       )
$$;
```

The function reads the HMAC secret from `tenant_context_secrets` (inaccessible to API/worker roles) and recomputes the signature server-side. Raw `current_setting()` is never used directly in policy expressions — only the HMAC-verified function result.

Workers set workspace_id, meta_mode, authorization_epoch, and signature via `apply_to_session()`. Fail closed: if any setting is missing or the HMAC doesn't match, the function returns `false` and no rows are visible or writable.

Cross-mode administrative queries use a separate database role with explicitly audited access. This role is only used for reconciliation and platform administration, never for normal domain operations.

## 3.29 Deferred Constraint Triggers

Cross-row constraints that CHECK cannot enforce:

1. **Internal validation credential requirement**: `internal_validation` connections must be backed by bindings with non-NULL `access_token_version_id`. Enforced by a deferred constraint trigger on both `MetaConnectionRecord.state` changes and `MetaDataSourceBindingRecord` mutations. The trigger verifies that at least one active binding exists with `access_token_version_id IS NOT NULL` when the connection's `meta_mode = 'internal_validation'`.

2. **Connection-workspace platform check**: `internal_validation` connections can only exist in platform workspaces. Enforced by a deferred constraint trigger on `MetaConnectionRecord` INSERT/UPDATE that queries `workspace_records.is_platform_workspace`.

---

# 4. Credential Resolution and Adapter Selection

## 4.1 Resolution Sequence

The credential resolution sequence determines adapter type *before* credential lookup:

1. **Determine adapter from mode.** Read `meta_mode` from the request/job context:
   - `customer_sandbox` → FakeMetaAdapter
   - `controlled_mock` → FakeMetaAdapter (with fault injection)
   - `internal_validation` → HttpMetaReadAdapter

2. **Resolve binding metadata.** Query `MetaDataSourceBindingRecord` by `(workspace_id, meta_mode, meta_connection_id, ad_account_id)` with `state = 'active'`. This resolves the binding's dual credential version refs (`access_token_version_id`, `app_secret_version_id`) and confirms the ad account association. For `customer_sandbox` and `controlled_mock`, both credential refs are NULL — no further credential lookup is needed.

3. **Resolve credential material** (internal_validation only). Dereference both credential version IDs through KMS envelope decryption using the existing `CredentialVersionRecord` infrastructure: decrypt `encrypted_secret` using `encrypted_data_key` (itself decrypted via `encryption_key_id`), `nonce`, and workspace-bound AAD (`aad_schema_version`). This yields the plaintext access token and app secret.

4. **Construct credential bundle.** The bundle contains both the access token (for API authentication) and app secret (for HMAC request signing). The bundle is ephemeral — it exists only for the duration of the API call and is never persisted. For `customer_sandbox` and `controlled_mock`, no bundle is constructed — the FakeMetaAdapter does not use credentials.

## 4.2 Intent Commit as Linearization Point

The `MetaCallIntentRecord` commit is the linearization point. It establishes authorization by acquiring `FOR SHARE` locks on the connection and binding within the same transaction. The intent and its corresponding attempt (saga step or page fetch) are created atomically:

```sql
-- Within the intent insertion transaction:
SELECT id FROM meta_connection_records
  WHERE id = :connection_id AND workspace_id = :workspace_id AND meta_mode = :meta_mode
  AND state = 'active'
  FOR SHARE;
  
SELECT id FROM meta_data_source_binding_records
  WHERE id = :binding_id AND workspace_id = :workspace_id AND meta_mode = :meta_mode
  AND state = 'active'
  FOR SHARE;

-- For internal_validation only: verify credential versions are active
SELECT id FROM credential_versions
  WHERE workspace_id = :workspace_id AND id = :access_token_version_id AND state = 'active'
  FOR SHARE;
SELECT id FROM credential_versions
  WHERE workspace_id = :workspace_id AND id = :app_secret_version_id AND state = 'active'
  FOR SHARE;

-- Intent + attempt inserted atomically
INSERT INTO meta_call_intent_records (...) VALUES (...);
-- Attempt references the intent:
INSERT INTO launch_saga_step_attempt_records (..., intent_id, ...) VALUES (..., :intent_id, ...);
  -- OR: INSERT INTO meta_page_fetch_attempt_records (..., intent_id, ...) VALUES (..., :intent_id, ...);
```

If any lock target is missing, inactive, or belongs to another workspace, the transaction fails. This prevents intents from being created against revoked credentials or retired connections. For `customer_sandbox` and `controlled_mock`, the credential FOR SHARE locks are skipped (credential version refs are NULL).

## 4.3 Multi-Binding Call Intent

When a single logical operation involves multiple bindings (e.g., cross-account queries), a `MetaCallIntentBindingRecord` child table links the intent to each binding:

```
MetaCallIntentBindingRecord:
  id: text PK
  intent_id: text NOT NULL, FK → MetaCallIntentRecord
  binding_id: text NOT NULL
  access_token_version_id: text | NULL
  app_secret_version_id: text | NULL
  
  Composite FK: (workspace_id, meta_mode, binding_id)
    → MetaDataSourceBindingRecord (workspace_id, meta_mode, id)
    where workspace_id and meta_mode are inherited from the parent intent
  Composite FK: (workspace_id, access_token_version_id)
    → credential_versions (workspace_id, id)
  Composite FK: (workspace_id, app_secret_version_id)
    → credential_versions (workspace_id, id)
  
  UNIQUE(intent_id, binding_id)
```

Each binding in the child table is locked with FOR SHARE during intent creation.

## 4.4 Bootstrap as Explicit Admin Command

Initial credential provisioning is an explicit platform-administration command, not an implicit startup operation:

1. Platform operator invokes `POST /api/platform/v1/meta/bootstrap` with workspace ID, mode, ad account, and credential material.
2. FastAPI creates `CredentialVersionRecord` rows using the existing schema from `app/entitlements/credentials.py` — one row with `credential_key = 'meta_access_token'` and one with `credential_key = 'meta_app_secret'`. Material is encrypted via KMS envelope (populating `encrypted_secret`, `encrypted_data_key`, `encryption_key_id`, `nonce`, `aad_schema_version`). Bootstrap idempotency uses the `(workspace_id, credential_key, version_no)` composite unique — if a row with `version_no = 1` already exists for the same workspace and key, the existing version is returned.
3. A `MetaConnectionRecord` and `MetaDataSourceBindingRecord` are created in the same transaction, with `access_token_version_id` and `app_secret_version_id` referencing the credential versions.

## 4.5 Per-Transaction RLS Context

Every database transaction sets tenant context via `apply_to_session()` from `app/platform/tenancy.py`:

```python
# Meta-domain operations include meta_mode in the context:
context = TenantContext(workspace_id=..., meta_mode=..., ...)
apply_to_session(session, context)
# apply_to_session writes all settings via SET LOCAL including:
#   marketai.workspace_id, marketai.meta_mode, marketai.authorization_epoch,
#   marketai.context_signature (HMAC covering workspace+epoch+mode)
# RLS policies verify the HMAC, not raw settings.
```

Workers receive `workspace_id` and `meta_mode` from the job payload. If either value is missing from the job, the worker fails closed without executing. The HMAC signature is computed by `apply_to_session()` using the extended payload that includes `meta_mode`.

## 4.6 Universal Fingerprint Recomputation

The request fingerprint (`request_fingerprint_hash`) on `MetaCallIntentRecord` is always recomputed from the canonical request identity at intent creation time — never accepted from external input. This ensures the fingerprint is consistent with the actual request parameters regardless of caller.

For environment-backed credentials, the HMAC-based provider idempotency key is similarly recomputed from the credential material at call time, not cached from a previous computation.

---

# 5. Launch Saga

## 5.1 Shared-Campaign Candidate Structure

Launch creates a single shared Campaign with N candidate AdSets and N Ads — one per frozen candidate. This is a shared-campaign candidate structure, not a native Meta A/B test (it does not use Meta's Experiments/Split Testing API or its audience-allocation semantics).

Key properties:
- Campaign-level budget optimization is disabled (`campaign_budget_optimization = false`).
- Each AdSet receives an identical daily budget (`daily_budget_minor` from the preview).
- The entire hierarchy is created in PAUSED status.
- Budget is sourced exclusively from the preview — not from any mutable state.
- All provisioning in #23 uses FakeMetaAdapter exclusively — no real Meta writes (§8.9).

## 5.2 Confirmation as Atomic Transaction

Preview confirmation uses the existing `LaunchConfirmationRecord` protocol (from `app/launch/service.py:213-270`):

**Confirmation step** (atomic transaction):

1. Validates the preview is in `draft` state and not expired.
2. Validates the `preview_hash` matches the stored hash (ensures the user confirmed exactly what they reviewed).
3. Takes structural experiment snapshot references: verifies the referenced `ExperimentVersionRecord` and all `ExperimentVersionCandidateRecord` rows still exist and are in the expected state.
4. Creates a `LaunchConfirmationRecord` with HMAC-signed token:
   - Token format: `confirm.{preview_id}.{nonce}.{HMAC-SHA256(LAUNCH_CONFIRMATION_SECRET, "{preview_id}:{preview_hash}:{nonce}")}`
   - Stores `token_hash = SHA-256(token)` on the confirmation record — the raw token is never stored.
   - Sets `expires_at` from `LAUNCH_CONFIRM_TTL_SECONDS`.
5. Transitions the preview to `confirmed` state.
6. Returns the token to the caller (one-time; never stored in plaintext).

**Execution step** (atomic consumption + provisioning creation):

1. Validates confirmation token by recomputing `token_hash = SHA-256(token)` and looking up `LaunchConfirmationRecord`.
2. If `consumed_at` is already set, returns the existing `LaunchProvisioningRecord` (idempotent replay).
3. Atomically sets `consumed_at = now()` on the confirmation.
4. Creates `LaunchProvisioningRecord` in the same transaction with `provisioning_state = 'pending'`.
5. Enqueues the provisioning job.

## 5.3 Saga Execution with Per-Call Persistence

Each Meta API call in the provisioning saga is persisted individually:

1. **Create intent + attempt** (atomic authorization transaction): Acquire FOR SHARE locks on connection, binding, and credential versions (§4.2). Insert `MetaCallIntentRecord` and `LaunchSagaStepAttemptRecord` atomically. The attempt's `intent_id` references the intent. Attempt starts in `state = 'intent_committed'`.
2. **Mark dispatch**: Set `intent.network_dispatched_at = now()` — this is the dispatch marker for uncertainty detection.
3. **Execute call**: Invoke the FakeMetaAdapter. All #23 provisioning uses FakeMetaAdapter exclusively — real Meta writes are deferred (§9).
4. **Record outcome**: Create `MetaCallOutcomeRecord` referencing the intent. Attempt transitions to `state = 'outcome_received'`.
5. **Apply result**: Update `LaunchProvisioningRecord` with remote IDs and advance provisioning state. Attempt transitions to `state = 'applied'`. This step uses CAS on the fencing token.

If the worker crashes between steps 2 and 4 (intent committed and dispatched, no outcome), the outcome is *missing*. The `network_dispatched_at IS NOT NULL` with no outcome makes the intent uncertain — it does not automatically become failed. Recovery inspection (§8.3) determines what happened.

## 5.4 Missing Outcomes and Uncertain State

When a call has an intent with `network_dispatched_at IS NOT NULL` but no outcome recorded:
- The intent is in an uncertain state.
- The worker does NOT transition to failed — the adapter call may have succeeded.
- Recovery (§8.3) inspects the adapter state to determine the actual result.
- Only when inspection produces conclusive evidence does the original operation transition to a terminal state.

## 5.5 Monotonic Fencing Tokens

The `LaunchProvisioningRecord.fencing_token` is a monotonically increasing integer. Each time a worker acquires a lease on a provisioning record, the fencing token is incremented atomically:

```sql
UPDATE launch_provisioning_records
SET fencing_token = fencing_token + 1, updated_at = now()
WHERE id = :provisioning_id AND fencing_token = :expected_token
RETURNING fencing_token;
```

The CAS check (`fencing_token = :expected_token`) ensures that a stale worker whose lease expired cannot overwrite a newer worker's progress.

## 5.6 Readbacks as Separate Calls

After creating each Meta object, the saga performs a separate readback call to verify the created object's state. The readback:

1. Is a separate `MetaCallIntentRecord` + `MetaCallOutcomeRecord` pair.
2. Carries step_type `'readback_campaign'`, `'readback_adset'`, etc.
3. Produces parity evidence that feeds into `LaunchParityCheckRecord`.

## 5.7 Activation Gate

After provisioning completes (all candidates have campaigns, ad sets, and ads), activation requires:

1. A `LaunchParityCheckRecord` with `passed = true` verifying:
   - All candidates present in Meta
   - Budget parity (identical daily budgets across ad sets)
   - Config parity (matching configurations)
   - Status parity (all PAUSED)
2. Human confirmation — the confirmation is a single set-level decision, even though activation requires status updates across multiple Meta objects.

In #23 scope, activation is deferred — all campaigns remain PAUSED. The activation gate design is preserved for forward compatibility but the `activating`/`active` states on `LaunchProvisioningRecord` are not reachable in #23. Activation failure preserves the campaign's PAUSED state rather than compensating through deletion.

## 5.8 FakeMetaAdapter

The FakeMetaAdapter is backed by durable `FakeMetaObjectRecord` rows, not in-memory state:

- Write operations create/update `FakeMetaObjectRecord` rows.
- Read operations query `FakeMetaObjectRecord` rows.
- Idempotency is guaranteed within a scenario version.
- Fault injection (for controlled_mock mode) is configured by the `ScenarioVersionRecord.scenario_config`.

---

# 6. Sync Pipeline, Observation Clock, and Reconciliation

## 6.1 Contract Hash

Every `ObservationContractGenerationRecord` stores a `contract_hash` — the SHA-256 of the canonical contract definition (fields, attribution window, breakdowns, time window, delivery config). This hash:

- Detects configuration changes that require a new generation.
- Does NOT serve as a unique constraint — different generations may have the same hash if a configuration was reverted. The generation number provides uniqueness.

## 6.2 Scheduler with SKIP LOCKED

The sync scheduler dispatches pending schedules using `SKIP LOCKED`:

```sql
SELECT id, workspace_id, meta_mode, contract_generation_id
FROM meta_sync_schedule_records
WHERE state = 'active' AND next_fire_at <= now()
ORDER BY next_fire_at
FOR UPDATE SKIP LOCKED
LIMIT :batch_size;
```

This allows multiple dispatcher instances to run concurrently without contention. Each dispatcher claims a disjoint set of ready schedules.

## 6.3 Page Fetch Attempt Records

Each page fetch within a sync run is tracked by a `MetaPageFetchAttemptRecord`. This provides:

- Retry counting per page.
- Crash recovery: if the worker crashes mid-page, the attempt record shows `state = 'started'` with no completion. The restarted worker creates a new attempt for the same page.
- Audit trail for individual API interactions.
- Intent linkage: each attempt carries `intent_id` referencing its authorization record.

## 6.4 Partial Recovery with Cursor Checkpoints

If a sync run fails mid-pagination:

1. The `MetaSyncRunRecord.last_completed_page_order` and `output_cursor` are updated after each successful page write.
2. On restart, the worker reads the last checkpoint and resumes from `output_cursor`.
3. Incomplete pages (started but not completed) are discarded — only fully committed pages contribute to the sync run.
4. A sync run with incomplete pagination cannot produce eligible observations or verdicts.

## 6.5 Terminal Page Atomicity

The final page of a pagination sequence and the sync run's transition to `succeeded` are written in the same database transaction:

```python
# In the same transaction:
session.add(MetaEvidencePageRecord(..., is_terminal=True))
sync_run.state = 'succeeded'
sync_run.completed_at = utcnow()
session.flush()
```

This prevents a state where the terminal page exists but the run is not marked complete, or vice versa.

## 6.6 14-Day Coverage Ledger

The observation clock for a contract generation requires 14 consecutive covered local dates:

1. Each complete, valid sync run backed by a valid attestation creates an `ObservationCoverageLedgerRecord` for the date it covers. The `coverage_date` is computed using the contract generation's `local_timezone`.
2. The `clock_started_at` on the contract generation is set when the first ledger entry is created — this is when coverage begins, not when the generation row was created.
3. Tenure is calculated by finding the longest streak of consecutive `coverage_date` values in the ledger.
4. The 14-day requirement means 14 consecutive calendar dates, not 14 runs or 14 * 24 hours.
5. Tenure tracking is derived from the ledger entries, not stored as a mutable counter.
6. Coverage entries reference their backing attestation. If an attestation is invalidated, its coverage entries are removed and the coverage streak is recalculated. This prevents invalidated evidence from counting toward the 14-day clock.

## 6.7 First-Generation Serialization

When creating the first generation for a `(workspace_id, meta_mode, meta_connection_id, contract_type)` scope, a `FOR UPDATE` lock cannot target a nonexistent row. Instead:

1. Acquire a transaction-scoped advisory lock keyed on the scope's hash.
2. Check for an existing active generation.
3. If none exists, create the first generation.
4. Release the advisory lock at transaction commit.

This prevents two concurrent transactions from both creating "first" generations.

## 6.8 Reconciliation Without Fabricating Facts

When a sync run's call evidence shows an uncertain intent (`network_dispatched_at IS NOT NULL`, no outcome), reconciliation does NOT fabricate a fake outcome with `network_attempted = false`. Instead:

1. Create a `MetaCallReconciliationRecord` linking the uncertain intent to a new inspection intent.
2. The inspection intent creates a real readback call to determine what happened.
3. The inspection's outcome is a genuine observation, not a fabricated result.
4. The reconciliation record stores the resolution: `confirmed_succeeded`, `confirmed_failed`, or `inconclusive`.

## 6.9 Append-Only Invalidation

Evidence invalidation is append-only:

1. An `EvidenceAttestationRecord` transitions from `valid` to `invalidated` — never back.
2. The invalidation reason and timestamp are recorded.
3. Taint propagation creates `ScaleVerdictTaintRecord` (or equivalent typed taint) for each downstream decision that depended on the invalidated evidence.
4. Taint resolution happens at run level: when new evidence is published for the same contract generation, all taints sourced from that generation's invalidated attestation are evaluated for resolution.
5. Coverage entries backed by the invalidated attestation are removed from the ledger (§6.6).

## 6.10 Publication Recovery

If a publication attempt fails terminally (e.g., validation discovered corrupted evidence), recovery creates a new publication generation:

```python
# New publication generation for the same sync run
publication = create_or_replay_operation(
    session,
    idempotency_key=f"epub:{sync_run_id}:gen:{next_generation}",
    command_type="evidence_publication",
    fingerprint_hash=compute_fingerprint(...),
)
```

This uses the existing `create_or_replay_operation` (from `app/platform/idempotency.py`) for crash-safe recovery of the publication operation itself.

## 6.11 Evidence Attestation Policy

When querying the current evidence for decisions: **highest evidence watermark wins** — `state = 'valid'`, `upper(evidence_watermark) DESC`, `published_at DESC` as tiebreaker.

This policy means:
- A run covering a more recent date range supersedes one covering an older range, regardless of when either was published.
- A restored historical run with an old watermark cannot become current if a valid attestation with a newer watermark exists — even if the historical run was re-published more recently.
- If all newer-watermark attestations are invalidated, the highest remaining valid watermark becomes current.

## 6.12 Verdict Supersession

When a new verdict is produced from newer evidence, a `VerdictSupersessionRecord` is created linking the old verdict to the new one. This prevents:

- Restored historical runs from resurrecting superseded verdicts.
- Multiple verdicts from being simultaneously "current" for the same scope.
- Deleted newer evidence from automatically promoting older verdicts.

A superseded verdict remains queryable for audit but does not participate in current decisions.

## 6.13 Occurrence CTE Safety

When inserting sync run occurrences, avoid `DO UPDATE SET id = EXCLUDED.id` which would change an existing PK:

```sql
-- Safe pattern:
WITH inserted AS (
  INSERT INTO meta_sync_run_records (id, occurrence_id, run_generation, ...)
  VALUES (:id, :occurrence_id, :run_generation, ...)
  ON CONFLICT (occurrence_id, run_generation) DO NOTHING
  RETURNING *
)
SELECT * FROM inserted
UNION ALL
SELECT * FROM meta_sync_run_records
WHERE occurrence_id = :occurrence_id AND run_generation = :run_generation
AND NOT EXISTS (SELECT 1 FROM inserted)
LIMIT 1;
```

---

# 7. Platform Workspace

## 7.1 Platform Workspace Flag

```sql
ALTER TABLE workspace_records ADD COLUMN is_platform_workspace boolean NOT NULL DEFAULT false;
```

Platform workspaces are owned and operated by MarketAI's internal team. They are not customer workspaces.

## 7.2 Platform Operator Membership

Only platform operators (members of the platform workspace with `platform_role` claims) can:

- Create `internal_validation` connections.
- Access platform-administration routes.
- Perform bootstrap credential provisioning.
- Execute cross-mode reconciliation.

Platform operators are not customer workspace members. They access platform routes through a separate authentication path (§8.7).

## 7.3 Internal Validation Scoping

`internal_validation` mode connections can only be created in workspaces where `is_platform_workspace = true`. This is enforced by:

1. A deferred constraint trigger on `MetaConnectionRecord` (§3.29).
2. Domain service validation at connection creation time.
3. RLS policy on `internal_validation` evidence restricting it to platform workspace scope.

---

# 8. BFF Contract, Operation Recovery, and Audit

## 8.1 Idempotency Boundary

Three distinct identifiers participate in command idempotency; each originates at a different layer:

**X-Intent-Id** (browser → BFF). The browser generates a UUIDv7 when the user initiates an action. Opaque to FastAPI; never crosses the BFF boundary. Preserved across browser retries of the same gesture.

**Idempotency-Key** (BFF → FastAPI). The BFF derives a deterministic key from X-Intent-Id, the resolved workspace UUID, and a fixed BFF namespace. Preserved across BFF retries. FastAPI receives this as the sole idempotency identity.

**Request fingerprint** (FastAPI-internal). Computed by the existing `compute_fingerprint(method, operation_id, path, query, body, precondition_headers)` in `app/platform/idempotency.py`. Stored alongside the idempotency record. Same key + same fingerprint = replay; same key + different fingerprint = 409 `idempotency_key_reused`. Never sent as a header.

**Sequencing.** The idempotency reservation/replay check runs *before* mutable-state validation:

1. Extract Idempotency-Key from request header.
2. Compute request fingerprint from canonical request identity.
3. `create_or_replay_operation` runs the reservation protocol: claim or replay.
4. Only on a new reservation (winner path) does the endpoint validate domain state, acquire locks, create intents, and dispatch jobs.
5. A replay returns the existing Operation without re-validation.

## 8.2 BFF Route Allowlist

Static, explicitly enumerated method/path pairs. No prefix-based forwarding. Each entry has a path template, HTTP method, and validator class.

**Workspace-scoped routes** (via delegated-user JWT):

| BFF path | Method | FastAPI path | Validator |
|---|---|---|---|
| `.../meta/connections` | POST | `/api/v1/meta/connections` | CreateConnectionValidator |
| `.../meta/connections/:id` | GET | `/api/v1/meta/connections/:id` | ResourceReadValidator |
| `.../meta/connections/:id/activate` | POST | `/api/v1/meta/connections/:id/activate` | StepUpValidator |
| `.../meta/connections/:id/retire` | POST | `/api/v1/meta/connections/:id/retire` | StepUpValidator |
| `.../meta/connections/:id/bindings` | GET | `/api/v1/meta/connections/:id/bindings` | PaginationValidator |
| `.../meta/connections/:id/bindings` | POST | `/api/v1/meta/connections/:id/bindings` | CreateBindingValidator |
| `.../meta/connections/:id/bindings/:bid/supersede` | POST | `/api/v1/meta/connections/:id/bindings/:bid/supersede` | SupersedeBindingValidator |
| `.../meta/contracts` | GET | `/api/v1/meta/contracts` | PaginationValidator |
| `.../meta/contracts` | POST | `/api/v1/meta/contracts` | CreateContractValidator |
| `.../meta/sync` | POST | `/api/v1/meta/sync` | IdempotentCommandValidator |
| `.../meta/sync/:id` | GET | `/api/v1/meta/sync/:id` | ResourceReadValidator |
| `.../meta/reconcile` | POST | `/api/v1/meta/reconcile` | IdempotentCommandValidator |
| `.../launch/previews` | POST | `/api/v1/launch/previews` | CreatePreviewValidator |
| `.../launch/previews/:id` | GET | `/api/v1/launch/previews/:id` | ResourceReadValidator |
| `.../launch/previews/:id/confirm` | POST | `/api/v1/launch/previews/:id/confirm` | ConfirmPreviewValidator |
| `.../launch/previews/:id/execute` | POST | `/api/v1/launch/previews/:id/execute` | IdempotentCommandValidator |
| `.../launch/operations/:id` | GET | `/api/v1/launch/operations/:id` | ResourceReadValidator |
| `.../launch/operations/:id/recovery` | POST | `/api/v1/launch/operations/:id/recovery` | IdempotentCommandValidator |
| `.../operations/:id` | GET | `/api/v1/operations/:id` | ResourceReadValidator |
| `.../audit` | GET | `/api/v1/audit` | PaginationValidator |

BFF path prefix: `/api/domain/v1/w/:slug/...`

**Platform-administration routes** (via platform-operator JWT, §8.7):

| Platform BFF path | Method | FastAPI path | Validator |
|---|---|---|---|
| `.../meta/bootstrap` | POST | `/api/platform/v1/meta/bootstrap` | BootstrapValidator |
| `.../meta/connections` | GET | `/api/platform/v1/meta/connections` | PaginationValidator |
| `.../meta/connections/:id` | GET | `/api/platform/v1/meta/connections/:id` | ResourceReadValidator |
| `.../meta/connections/:id/reconcile` | POST | `/api/platform/v1/meta/connections/:id/reconcile` | IdempotentCommandValidator |
| `.../credentials/:id/revoke` | POST | `/api/platform/v1/credentials/:id/revoke` | StepUpValidator |

Platform BFF path prefix: `/api/platform/v1/...`

Each validator enforces request-shape constraints (headers, body schema, parameter ranges) at the BFF. `StepUpValidator` additionally extracts the step-up assertion header. `IdempotentCommandValidator` ensures the Idempotency-Key header is present. `BootstrapValidator` validates credential material shape and workspace eligibility. No validator performs domain-state validation.

## 8.3 Operation Recovery

When a command Operation reaches `uncertain`, it is eligible for recovery.

**Recovery Operation.** Recovery creates a *separate* Operation linked to the original via `LaunchReconciliationRecord` (§3.21).

**Recovery sequencing (worker behavior):**

1. Worker picks up recovery job, transitions recovery Operation: `queued` → `running`.
2. Recovery Operation `running`: worker inspects adapter state — reads back objects from FakeMetaAdapter (or Meta API for internal_validation), compares against last known intent/outcome evidence from the original Operation.
3. **Conclusive result**: inspection finds definitive evidence. Worker:
   - Persists inspection evidence (`MetaCallOutcomeRecord` with `inspected_operation_id` set to original).
   - Transitions *original* Operation to the indicated terminal state.
   - Transitions recovery Operation to `succeeded`.
4. **Inconclusive result**: inspection cannot determine what happened. Worker:
   - Persists inspection attempt evidence.
   - Transitions recovery Operation to `failed` with reason "inconclusive."
   - Original Operation remains `uncertain` — eligible for another recovery attempt.

**What does NOT trigger uncertain.** Lease expiry alone does not transition to `uncertain`. When a worker's lease expires, the fenced job is redelivered to a worker with a valid (monotonically higher) fencing token. Only when call evidence shows a dispatched effect whose completion is unknown (`network_dispatched_at IS NOT NULL`, no outcome received) does the worker transition `running` → `uncertain`.

**Recovery vs. retry.** Recovery is inspection — it reads adapter state. It never re-executes write effects.

## 8.4 Launch Reconciliation Serialization

`LaunchReconciliationRecord` (§3.21) with partial unique index `UNIQUE(original_operation_id) WHERE state = 'open'` ensures at most one open reconciliation per original Operation. A new attempt requires the previous one to resolve first.

## 8.5 Scenario-Version Invariant

Composite FK chain ensures controlled_mock call intents reference scenarios belonging to the same connection:

```
MetaCallIntentRecord
  → (workspace_id, meta_mode, meta_connection_id, scenario_version_id)
  → ScenarioVersionRecord (workspace_id, meta_mode, meta_connection_id → connection FK, id)
```

CHECK constraint on `MetaCallIntentRecord`:
- `internal_validation` → `scenario_version_id IS NULL`
- `customer_sandbox` or `controlled_mock` → `scenario_version_id IS NOT NULL`

For `controlled_mock` creation: request body includes `scenario_version_id`, or server selects the connection's current default scenario version. If neither exists, the request fails.

## 8.6 Attribution Model

**Canonical executor.** Every state-changing record carries `executor` as a namespaced identity string:

| Prefix | Example | Meaning |
|---|---|---|
| `user:` | `user:usr_abc123` | Human user via BFF |
| `worker:` | `worker:launch-provisioner` | Named worker process |
| `scheduler:` | `scheduler:meta-sync` | Scheduled job dispatcher |
| `platform:` | `platform:credential-admin` | Platform operator via admin routes |
| `system:` | `system:reconciler` | System-initiated background process |

`initiator_id` (original human) is preserved separately and never overwritten.

**Three distinct hash values:**

1. **Command idempotency key hash** (`idempotency_key_hash` on `idempotency_records`): SHA-256 of BFF-derived Idempotency-Key. Scoped to `(key_hash, workspace_id)`. Prevents duplicate command execution.

2. **Meta request fingerprint** (`request_fingerprint_hash` on `MetaCallIntentRecord`): SHA-256 of canonical Meta API request identity. Supports adapter-level deduplication and reconciliation matching.

3. **Provider idempotency key** (`provider_idempotency_key` on `MetaCallIntentRecord`): HMAC-SHA256 value sent to Meta's API for their dedup. Deterministically derived from connection secret + canonical request identity.

**Recovery evidence cross-reference.** Recovery Operation's `MetaCallOutcomeRecord` carries both `operation_id` (recovery) and `inspected_operation_id` (original uncertain). Full chain: original intent → original uncertain outcome → recovery inspection → resolution.

## 8.7 Platform Administration and Credential Lifecycle

**Platform-operator JWT.** Separate from the delegated-user JWT:
- `typ: "platform+jwt"`, distinct issuer/audience pair.
- Claims: `operator_id`, `platform_role`, standard temporal claims.
- Validated against a separate JWKS endpoint.
- Operator must be a member of the platform workspace.
- Routes on `/api/platform/v1/...` with their own BFF allowlist (§8.2).

**Step-up for credential changes.** Operations that modify credential state require step-up assertion verification, extending `app/platform/step_up.py`. The `action` claim must match the specific operation: `activate_meta_connection`, `revoke_credential_version`, `supersede_meta_binding`, etc.

**CredentialVersion.state:** `active` → credential usable; `rotated` → superseded by newer version; `revoked` → fail closed at authorization time. Revocation is append-only. Rotation = new version + transition old to rotated in single transaction.

**MetaConnectionRecord lifecycle:**
```
active → retiring   (non-terminal provisionings exist)
active → retired    (no non-terminal provisionings)
retiring → retired  (all provisionings terminal)
```
Connection creation returns `active` immediately. No `provisioning` state.

**Binding supersession.** `POST .../bindings/:bid/supersede` atomically:
1. Validates new binding's scope matches old binding's scope.
2. Creates new binding as `active`.
3. Transitions old binding to `superseded` with `superseded_by_id`.
4. Verifies new binding's credential versions are active.

No window where workspace lacks an active binding for the scope.

**Retirement guard.** Before `active` → `retiring`:
1. Check for non-terminal `LaunchProvisioningRecord` rows. If any, enter `retiring`.
2. Pause active sync schedules (not delete).
3. Mark bindings `supersession_required`.
4. Background process monitors provisioning completion, transitions `retiring` → `retired`.

## 8.8 Audit Trail

Append-only `AuditEventRecord` (§3.27). BFF surfaces via read-only paginated endpoint (`.../audit` GET, §8.2) with filtering by operation, resource, initiator, executor, event type, meta_mode, and time range. BFF cannot create or modify audit records.

## 8.9 Write-Client Isolation Proof

No Meta write adapter can be instantiated in #23 scope. Four independent guarantees enforce this:

1. **No write adapter class.** `HttpMetaWriteAdapter` does not exist in #23 — it is not implemented. The only HTTP adapter is `HttpMetaReadAdapter`, which is restricted to read-only endpoints.

2. **FakeMetaAdapter is database-local.** The FakeMetaAdapter's "write" operations create/update `FakeMetaObjectRecord` rows in the local database. No network call is ever made. The adapter's `is_test_adapter = True` property is verified at the call boundary.

3. **Schema-level write prohibition.** `MetaCallIntentRecord` carries `CHECK: write_permitted = false` — the database rejects any intent that claims write permission. This constraint cannot be bypassed by application code.

4. **Environment guard.** `META_WRITE_ACCESS_TOKEN` remains empty (CLAUDE.md invariant: "Meta write access remains disabled in development"). The existing `meta_write_ready()` guard in `app/launch/service.py` refuses to proceed when this token is empty.

The combination of no write adapter class, database-level CHECK constraint, and environment guard provides defense in depth. Real Meta writes are deferred beyond #23 scope (§9).

---

# 9. Decisions Deferred

The following are explicitly out of scope for #23:

1. **Full per-workspace token onboarding, rotation, and KMS lifecycle** — deferred to #9. #23 uses environment-backed bootstrap credentials represented by existing `CredentialVersionRecord` rows from `app/entitlements/credentials.py`.

2. **WebSocket/SSE for real-time updates** — deferred. HTTP polling with `ETag` and `Retry-After` is sufficient until proven otherwise.

3. **Customer real Meta writes** — deferred. `write_permitted = false` on all #23 intents. `META_WRITE_ACCESS_TOKEN` stays empty. Scale never performs Meta writes (ADR 0008). No `HttpMetaWriteAdapter` class exists in #23.

4. **Autopilot / automatic Meta delivery mutations** — explicitly rejected by ADR 0008. Scale may persist health, advice, and dispositions, but never owns or calls Meta delivery or budget mutations.

5. **Meta Experiments/Split Testing API** — the Launch candidate structure uses shared campaigns with per-AdSet budgets, not Meta's native A/B testing. Native A/B would require audience-allocation semantics not in scope.

6. **Launch activation** — all campaigns remain PAUSED in #23. The activation gate design (§5.7) is preserved for forward compatibility but `activating`/`active` provisioning states are not reachable.

---

# 10. Existing Code References

This design extends the following existing modules:

- **`app/platform/tenancy.py`** — `TenantContext`, `tenant_context_signature()`, `apply_to_session()`, `ALL_SETTINGS`. Extended with `meta_mode` as a new setting (`marketai.meta_mode`) and included in the HMAC signature payload. The `marketai_meta_context_is_current()` PostgreSQL function follows the same SECURITY DEFINER pattern as the existing `marketai_context_is_current()`.

- **`app/entitlements/credentials.py`** — `CredentialVersionRecord` with KMS envelope encryption (encrypted_secret, encrypted_data_key, encryption_key_id, nonce, aad_schema_version) and three-state lifecycle. `CredentialBindingRecord` with composite FK enforcement. Used as-is — no schema changes.

- **`app/platform/operations.py`** — Operation state machine with `VALID_TRANSITIONS`, `TERMINAL_STATES`, `create_operation`, `transition_operation`. Recovery operations use the same state machine; the `uncertain` → `running` backward transition is reserved for reconcilers.

- **`app/platform/idempotency.py`** — `compute_fingerprint`, `hash_idempotency_key`, `create_or_replay_operation`, `create_or_replay_intent`. Evidence publication recovery (§6.10) uses `create_or_replay_operation`. The pending-reservation protocol is the foundation for all idempotent command handling.

- **`app/platform/step_up.py`** — `validate_step_up_assertion`, `PROTECTED_OPERATIONS`, `StepUpClaims`. Extended with new protected operations for credential management: `activate_meta_connection`, `revoke_credential_version`, `supersede_meta_binding`, `retire_meta_connection`.

- **`app/adapters/persistence/models.py`** — `AuditEventRecord` (extended with `meta_mode` column), `LaunchConfirmationRecord` (used as-is for confirmation protocol), `LaunchOperationRecord` (replaced by `LaunchProvisioningRecord`), `LaunchStepRecord` (replaced by `LaunchSagaStepAttemptRecord`).

- **`app/meta/service.py`** — `require_meta_credential_workspace()` is replaced by the credential resolution sequence (§4). `MetaClient` global config reads are replaced by workspace-scoped adapter resolution.

- **`app/launch/service.py`** — Single-entry saga (`REMOTE_STEPS`) is replaced by the multi-candidate provisioning saga (§5). The confirmation protocol (HMAC-signed token, atomic consumption, LaunchConfirmationRecord) is preserved and extended.

- **`docs/architecture/saas-ui-fastapi-integration-contract.md`** — Route conventions (§4, §7.2): BFF prefix `/api/domain/v1/w/{workspace_slug}/*`, FastAPI prefix `/api/v1/*`. Idempotency semantics (§7.2): `Idempotency-Key` required for commands with external calls. The BFF route allowlist (§8.2) follows the explicit-allowlist pattern described in the contract.

- **`docs/adr/0003-enforce-workspace-identity-boundary.md`** — "FastAPI derives tenant and role from verified claims and enforces its own policy." Domain service must enforce `internal_validation` restriction, not just BFF.

- **`docs/adr/0008-separate-scale-evidence-advice-and-delivery-control.md`** — Scale never owns/calls Meta delivery or budget mutation. Conservative maturity, mode isolation, episode-reset gates. "No autopilot" decision.

---

# 11. ADR and Glossary Updates

- **ADR 0009** (`docs/adr/0009-three-meta-evidence-modes-and-platform-workspace.md`): Committed at `64e840e`. Covers three Meta evidence modes, Platform Workspace concept, mode isolation as database invariant (RLS + composite FKs), four rejected alternatives.

- **CONTEXT.md glossary**: Committed at `5427845`. Added Platform Workspace, Canonical Executor, Launch Candidate Manifest, Meta Evidence Mode, Observation Contract Generation. Revised Meta Sandbox to name all three modes with network behavior and decision eligibility.
