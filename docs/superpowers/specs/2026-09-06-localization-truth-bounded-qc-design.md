# Localization Truth, Bounded QC, and Asset Lineage — Design Spec

Issue: [#24](https://github.com/heart-cannot-beat/marketai-archive/issues/24)
Date: 2026-09-06
Contract: `docs/product/localization-truth-package-contract.md`
ADR: ADR 0006

## Scope

This spec covers all persistent domain objects, module interfaces, adapter seams, worker pipeline behavior, and acceptance evidence required by Contract §1–§18 for the Localization feature area. It introduces 32 new tables, evolves 2 existing tables (`asset_versions`, `provider_call_attempts`), adds the `ENTRY_BRAND_LOCALIZATION_POLICY` library type, generalizes the provider-dispatch module, and defines a three-layer testing strategy.

The implementation ships two fact-source adapters (Manual, Controlled Mock) and no Shopify/Claim/Shipping stubs. Future adapters implement the same seam when their tickets land.

---

# Section 1: Domain Model and Tables

## 1.1 Cross-cutting conventions

Every workspace-scoped table follows these rules:

- `workspace_id: String(64)` FK to `workspaces.id`, NOT NULL, indexed.
- `UNIQUE(workspace_id, id)` on every table, providing a composite target for tenant-safe foreign keys from child tables.
- All composite foreign keys include `workspace_id` so that a row in Workspace A can never reference a row in Workspace B, even without RLS.
- PostgreSQL RLS policies and FORCE RLS enforce tenant isolation at the database level. SQLite skips RLS but retains composite FK safety.
- Immutable tables receive a PostgreSQL trigger that rejects `UPDATE` and `DELETE` on all columns except system housekeeping (e.g. `updated_at` on CAS projections).
- CAS (Compare-and-Swap) projections use a `revision: Integer` column. Writers read the current revision, increment it, and include `WHERE revision = :expected` in the UPDATE. A losing writer reloads rather than overwriting.
- Partial unique indexes enforce "one effective/active version" semantics where noted.
- `new_id(prefix)` generates `{prefix}_{uuid4_hex}` identifiers.
- `utcnow()` returns `datetime.now(timezone.utc)`.

Platform-scoped tables (market policies) have NO `workspace_id` and are shared across all workspaces.

## 1.2 Platform-scoped tables

### `market_policies`

One row per target market in the platform catalog. Not workspace-scoped.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `market_id` | String(10) | NOT NULL, UNIQUE — stable human-readable identifier (e.g. `en-GB`) |
| `locale` | String(10) | NOT NULL |
| `country` | String(2) | NOT NULL — ISO 3166-1 alpha-2 |
| `currency` | String(3) | NOT NULL — ISO 4217 |
| `created_at` | DateTime(tz) | NOT NULL |

Nine rows for the v9 Beta catalog: `en-GB`, `en-AU`, `en-CA`, `de-DE`, `fr-FR`, `es-ES`, `it-IT`, `ja-JP`, `ko-KR`.

### `market_policy_versions`

Immutable versioned snapshots of market policy rules. Not workspace-scoped.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `market_policy_id` | String(64) | FK → `market_policies.id`, NOT NULL |
| `version_number` | Integer | NOT NULL |
| `version_state` | String(20) | CHECK IN (`'effective'`, `'superseded'`), NOT NULL |
| `locale` | String(10) | NOT NULL |
| `country` | String(2) | NOT NULL |
| `currency` | String(3) | NOT NULL |
| `pricing_context_json` | JSON | NOT NULL — Shopify pricing context, price list rules |
| `digit_format_json` | JSON | NOT NULL — decimal separator, grouping, currency position |
| `base_tone` | String(40) | NOT NULL |
| `reserved_words_json` | JSON | NOT NULL — do-not-translate list |
| `restricted_claim_types_json` | JSON | NOT NULL — claim types that require special handling |
| `requires_local_copy` | Boolean | NOT NULL |
| `shipping_address_granularity` | String(20) | CHECK IN (`'country'`, `'region'`, `'postal_code'`), NOT NULL |
| `max_fact_staleness_seconds` | Integer | NOT NULL |
| `qc_rules_json` | JSON | NOT NULL — per-market QC configuration |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(market_policy_id, version_number)`
- `UNIQUE(id, market_policy_id)` — parent-carrying target for policy events
- Partial unique: `UNIQUE(market_policy_id) WHERE version_state = 'effective'` — at most one effective version per market policy at any time.
- Immutable trigger on all columns EXCEPT `version_state`, which permits only the single forward transition `effective → superseded` via a PostgreSQL trigger that rejects any other UPDATE.

### `market_policy_version_events`

Append-only platform event log and transactional outbox for MarketPolicy publication and supersession. The state transition and its event commit in the same transaction. Workers may replay the log; downstream deduplication uses the event ID.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `market_policy_id` | String(64) | FK → `market_policies.id`, NOT NULL |
| `market_policy_version_id` | String(64) | FK → `market_policy_versions.id`, NOT NULL |
| `sequence` | Integer | NOT NULL, CHECK `sequence >= 1` |
| `event_type` | String(20) | CHECK IN (`'published'`, `'superseded'`), NOT NULL |
| `previous_state` | String(20) | nullable |
| `current_state` | String(20) | CHECK IN (`'effective'`, `'superseded'`), NOT NULL |
| `deduplication_key` | String(128) | NOT NULL |
| `actor_id` | String(100) | NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(market_policy_id, sequence)`
- `UNIQUE(market_policy_version_id, event_type)` — at most one publication and one supersession event per version
- `UNIQUE(deduplication_key)`
- `UNIQUE(id, market_policy_version_id)` — composite target for downstream event evidence
- Composite FK `(market_policy_version_id, market_policy_id)` → `market_policy_versions(id, market_policy_id)` — proves the event's version belongs to the same policy
- CHECK: `(event_type = 'published' AND previous_state IS NULL AND current_state = 'effective') OR (event_type = 'superseded' AND previous_state = 'effective' AND current_state = 'superseded')`
- PostgreSQL DEFERRABLE INITIALLY DEFERRED constraint triggers require one matching `published` event for each newly inserted effective version and one matching `superseded` event for each state transition by commit; the platform publisher provides the same transaction boundary on SQLite
- Append-only trigger; platform publisher is the only writer

## 1.3 Truth resolution tables

### `truth_packages`

Immutable, workspace-scoped. One package per batch resolution. Contains the canonical hash for idempotent reuse.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `product_version_id` | String(64) | NOT NULL |
| `offer_version_id` | String(64) | NOT NULL |
| `truth_resolution_policy_version_id` | String(64) | NOT NULL |
| `truth_data_mode` | String(20) | CHECK IN (`'real'`, `'mock'`), NOT NULL |
| `canonical_hash` | String(64) | NOT NULL — SHA-256 over all frozen inputs, modes, evidence, derivation rules |
| `status` | String(20) | CHECK IN (`'full'`, `'degraded'`, `'failed'`), NOT NULL |
| `valid_until` | DateTime(tz) | NOT NULL — earliest expiry across all facts |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, id, truth_data_mode)` — exact provenance target for Lineage
- `UNIQUE(workspace_id, canonical_hash)` — idempotent reuse
- Composite FK `(workspace_id, product_version_id)` → `library_versions(workspace_id, id)`
- Composite FK `(workspace_id, offer_version_id)` → `library_versions(workspace_id, id)`
- Composite FK `(workspace_id, truth_resolution_policy_version_id)` → `library_versions(workspace_id, id)`
- Immutable trigger

### `truth_package_markets`

One row per target market within a truth package. Workspace-scoped, immutable.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `truth_package_id` | String(64) | NOT NULL |
| `market_policy_version_id` | String(64) | FK → `market_policy_versions.id`, NOT NULL (platform FK, no workspace) |
| `market` | String(10) | NOT NULL — denormalized for queries |
| `readiness` | String(20) | CHECK IN (`'ready'`, `'degraded'`, `'failed'`, `'unavailable'`), NOT NULL |
| `readiness_detail_json` | JSON | NOT NULL — per-fact status and warnings |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)` — composite FK target
- `UNIQUE(workspace_id, truth_package_id, market)` — one row per market per package
- `UNIQUE(workspace_id, id, truth_package_id)`
- `UNIQUE(workspace_id, id, truth_package_id, market_policy_version_id)`
- `UNIQUE(workspace_id, id, truth_package_id, market, market_policy_version_id)` — parent-carrying target for Facts, dependencies, CopyVersions, and Items
- Composite FK `(workspace_id, truth_package_id)` → `truth_packages(workspace_id, id)`
- PostgreSQL INSERT trigger requires `market` to equal the `market_policies.market_id` reached through `market_policy_version_id`; `truth.py` enforces the same rule on SQLite
- Immutable trigger

### `truth_source_attempts`

Immutable record of every source resolution attempt, including failed and skipped attempts. Enables auditable fallback provenance per §2A.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `truth_package_id` | String(64) | NOT NULL — denormalized parent identity |
| `truth_package_market_id` | String(64) | NOT NULL |
| `fact_type` | String(40) | NOT NULL |
| `scope_key` | String(120) | NOT NULL — concrete component/claim identity, not generic labels |
| `priority_ordinal` | Integer | NOT NULL — position in policy `source_priority` |
| `data_source_version_id` | String(64) | NOT NULL |
| `market_source_mapping_version_id` | String(64) | nullable |
| `adapter_kind` | String(40) | NOT NULL |
| `adapter_version` | String(40) | NOT NULL — exact adapter implementation version |
| `manual_fact_set_version_id` | String(64) | nullable — present when adapter_kind is manual |
| `resolution_rule_version` | String(40) | NOT NULL — truth resolution rule version used |
| `truth_data_mode` | String(20) | CHECK IN (`'real'`, `'mock'`), NOT NULL |
| `requested_context_json` | JSON | NOT NULL — market/offer/mapping context sent to adapter |
| `mock_scenario` | String(100) | nullable — present for mock adapters |
| `mock_version` | String(40) | nullable — present for mock adapters |
| `outcome` | String(20) | CHECK IN (`'accepted'`, `'failed'`, `'skipped'`), NOT NULL |
| `failure_code` | String(60) | NOT NULL when `outcome = 'failed'`, NULL otherwise |
| `skip_code` | String(60) | NOT NULL when `outcome = 'skipped'`, NULL otherwise |
| `evidence_hash` | String(64) | NOT NULL when `outcome = 'accepted'`, nullable otherwise |
| `started_at` | DateTime(tz) | NOT NULL |
| `completed_at` | DateTime(tz) | NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, truth_package_market_id, fact_type, scope_key, priority_ordinal)` — one attempt per policy slot
- `UNIQUE(workspace_id, id, truth_package_id, truth_package_market_id, fact_type, scope_key)` — composite FK target for `truth_facts`
- Composite FK `(workspace_id, truth_package_market_id, truth_package_id)` → the corresponding parent-carrying key on `truth_package_markets`
- Composite FK `(workspace_id, data_source_version_id)` → `library_versions(workspace_id, id)`
- Composite FK `(workspace_id, market_source_mapping_version_id)` → `library_versions(workspace_id, id)` when NOT NULL
- Composite FK `(workspace_id, manual_fact_set_version_id)` → `library_versions(workspace_id, id)` when NOT NULL
- CHECK: `(outcome = 'failed' AND failure_code IS NOT NULL) OR (outcome != 'failed' AND failure_code IS NULL)`
- CHECK: `(outcome = 'skipped' AND skip_code IS NOT NULL) OR (outcome != 'skipped' AND skip_code IS NULL)`
- CHECK: `(outcome = 'accepted' AND evidence_hash IS NOT NULL) OR (outcome IN ('failed', 'skipped') AND evidence_hash IS NULL)`
- CHECK: `(adapter_kind = 'manual' AND manual_fact_set_version_id IS NOT NULL AND truth_data_mode = 'real') OR (adapter_kind != 'manual' AND manual_fact_set_version_id IS NULL)`
- CHECK: `(truth_data_mode = 'mock' AND mock_scenario IS NOT NULL AND mock_version IS NOT NULL) OR (truth_data_mode = 'real' AND mock_scenario IS NULL AND mock_version IS NULL)`
- Immutable trigger

### `truth_facts`

Immutable raw facts resolved from sources. Each fact traces back to its accepted source attempt.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `truth_package_id` | String(64) | NOT NULL — denormalized parent identity |
| `truth_package_market_id` | String(64) | NOT NULL |
| `fact_type` | String(40) | NOT NULL |
| `scope_key` | String(120) | NOT NULL |
| `accepted_source_attempt_id` | String(64) | NOT NULL |
| `value_json` | JSON | NOT NULL — typed `FactValue` (MoneyFactValue, AvailabilityFactValue, etc.) |
| `evidence_hash` | String(64) | NOT NULL |
| `external_object_ref` | String(256) | NOT NULL |
| `requested_context_json` | JSON | NOT NULL — preserved from adapter request for audit |
| `mock_scenario` | String(100) | nullable |
| `mock_version` | String(40) | nullable |
| `observed_at` | DateTime(tz) | NOT NULL |
| `valid_until` | DateTime(tz) | NOT NULL — earliest of source validity, policy staleness, market staleness |
| `source_valid_until` | DateTime(tz) | nullable — source-reported validity |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, id, truth_package_id)` — same-package dependency target
- `UNIQUE(workspace_id, id, truth_package_id, truth_package_market_id)` — same-market derived-input target
- `UNIQUE(workspace_id, id, truth_package_id, truth_package_market_id, scope_key)` — same-market/scope Claim replacement target
- `UNIQUE(workspace_id, id, truth_package_id, truth_package_market_id, fact_type, scope_key)` — complete typed Fact identity
- Composite FK `(workspace_id, truth_package_market_id, truth_package_id)` → the corresponding parent-carrying key on `truth_package_markets`
- Composite FK `(workspace_id, accepted_source_attempt_id, truth_package_id, truth_package_market_id, fact_type, scope_key)` → `truth_source_attempts(workspace_id, id, truth_package_id, truth_package_market_id, fact_type, scope_key)` — enforces the complete accepted-attempt scope
- PostgreSQL trigger on INSERT verifies that the referenced `truth_source_attempts.outcome = 'accepted'`; rejects insertion otherwise
- PostgreSQL trigger verifies that duplicated requested-context and Mock provenance equal the referenced accepted attempt; SQLite enforces the same invariant through the `truth.py` interface tests
- Immutable trigger

### `truth_derived_facts`

Immutable derived facts computed deterministically from raw facts.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `truth_package_id` | String(64) | NOT NULL — denormalized parent identity |
| `truth_package_market_id` | String(64) | NOT NULL |
| `derived_type` | String(40) | NOT NULL — e.g. `'formatted_price'`, `'stacked_discount'`, `'max_claimable_discount'` |
| `scope_key` | String(120) | NOT NULL |
| `value_json` | JSON | NOT NULL |
| `derivation_rule_version` | String(40) | NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, id, truth_package_id, truth_package_market_id)` — parent-carrying input target
- Composite FK `(workspace_id, truth_package_market_id, truth_package_id)` → the corresponding parent-carrying key on `truth_package_markets`
- Immutable trigger

### `truth_derived_fact_inputs`

Links derived facts to their input raw facts. Immutable.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `truth_package_id` | String(64) | NOT NULL |
| `truth_package_market_id` | String(64) | NOT NULL |
| `derived_fact_id` | String(64) | NOT NULL |
| `input_fact_id` | String(64) | NOT NULL |

Constraints:
- Composite FK `(workspace_id, derived_fact_id, truth_package_id, truth_package_market_id)` → `truth_derived_facts(workspace_id, id, truth_package_id, truth_package_market_id)`
- Composite FK `(workspace_id, input_fact_id, truth_package_id, truth_package_market_id)` → the same-market parent-carrying key on `truth_facts`
- `UNIQUE(workspace_id, derived_fact_id, input_fact_id)`
- Immutable trigger

### `fact_library_dependencies`

Maps truth facts/packages to their `library_versions` dependencies for stale propagation. Uses a typed composite FK to `library_versions` instead of an untyped version ID string.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `truth_package_id` | String(64) | NOT NULL |
| `truth_fact_id` | String(64) | nullable — NULL for package-level deps (ProductVersion, OfferVersion, TruthResolutionPolicyVersion) |
| `dependency_kind` | String(40) | CHECK IN (`'product_version'`, `'offer_version'`, `'data_source_version'`, `'manual_fact_set_version'`, `'market_source_mapping_version'`, `'truth_resolution_policy_version'`), NOT NULL |
| `dependency_version_id` | String(64) | NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- Partial unique `UNIQUE(workspace_id, truth_package_id, truth_fact_id, dependency_kind, dependency_version_id) WHERE truth_fact_id IS NOT NULL` — Fact-level dependency deduplication
- Partial unique `UNIQUE(workspace_id, truth_package_id, dependency_kind, dependency_version_id) WHERE truth_fact_id IS NULL` — package-level dependency deduplication with explicit NULL semantics
- Composite FK `(workspace_id, truth_package_id)` → `truth_packages(workspace_id, id)`
- Composite FK `(workspace_id, truth_fact_id, truth_package_id)` → `truth_facts(workspace_id, id, truth_package_id)` when `truth_fact_id` is NOT NULL
- Composite FK `(workspace_id, dependency_version_id)` → `library_versions(workspace_id, id)` — typed FK to actual library_versions table
- CHECK: `(truth_fact_id IS NULL AND dependency_kind IN ('product_version', 'offer_version', 'truth_resolution_policy_version')) OR (truth_fact_id IS NOT NULL AND dependency_kind IN ('data_source_version', 'manual_fact_set_version', 'market_source_mapping_version'))`
- PostgreSQL trigger joins the referenced `library_versions.entry_id` to `library_entries.entry_type` and requires the exact mapping `product_version→product`, `offer_version→offer`, `data_source_version→data_source`, `manual_fact_set_version→manual_fact_set`, `market_source_mapping_version→market_source_mapping`, or `truth_resolution_policy_version→truth_resolution_policy`; the `truth.py` interface performs the same check on SQLite
- Immutable trigger

### `fact_market_policy_dependencies`

Maps truth packages to their platform-scoped `market_policy_version` dependencies. Separate table because `market_policy_versions` has no `workspace_id`, so a standard workspace-composite FK is impossible.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `truth_package_id` | String(64) | NOT NULL |
| `truth_package_market_id` | String(64) | NOT NULL — the specific market this policy applies to |
| `market_policy_version_id` | String(64) | FK → `market_policy_versions.id`, NOT NULL (platform FK, no workspace) |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, truth_package_id, truth_package_market_id, market_policy_version_id)`
- Composite FK `(workspace_id, truth_package_id)` → `truth_packages(workspace_id, id)`
- Composite FK `(workspace_id, truth_package_market_id, truth_package_id, market_policy_version_id)` → the corresponding parent-carrying key on `truth_package_markets`
- Immutable trigger

Stale propagation is driven by two event sources: (1) version lifecycle records in the existing append-only `library_events` stream (`effective → superseded/withdrawn`), consumed by an idempotent worker that queries `fact_library_dependencies`; (2) platform events in `market_policy_version_events`, consumed by the same worker querying `fact_market_policy_dependencies`. Both produce `market_asset_binding_events` for affected active bindings.

## 1.4 Source claim tables

### `source_claim_inventories`

Immutable versioned observations of source AssetVersion claims. Workspace-scoped.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `asset_version_id` | String(64) | NOT NULL |
| `analysis_recipe_hash` | String(64) | NOT NULL — deterministic recipe identity |
| `version_number` | Integer | NOT NULL |
| `claims_json` | JSON | NOT NULL — array of typed observations: claim_type, ocr_text, region, confidence, analysis provider/model/rule, evidence, confirmation status |
| `provider` | String(64) | NOT NULL |
| `model` | String(200) | NOT NULL |
| `rule_version` | String(40) | NOT NULL |
| `analysis_execution_mode` | String(20) | CHECK IN (`'byok'`, `'sandbox'`, `'mock'`), NOT NULL |
| `adapter_version` | String(40) | NOT NULL |
| `mock_scenario` | String(100) | nullable — present for mock adapters |
| `mock_version` | String(40) | nullable — present for mock adapters |
| `evidence_hash` | String(64) | NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, id, asset_version_id)` — proves an Inventory belongs to an exact source version
- `UNIQUE(workspace_id, id, asset_version_id, analysis_execution_mode)` — preparation-result mode target
- `UNIQUE(workspace_id, asset_version_id, analysis_recipe_hash, version_number)`
- Composite FK `(workspace_id, asset_version_id)` → `asset_versions(workspace_id, id)`
- Immutable trigger

### `source_claim_confirmations`

Append-only user confirmations for individual claims within an inventory.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `inventory_id` | String(64) | NOT NULL |
| `claim_index` | Integer | NOT NULL — position within `claims_json` |
| `confirmation_kind` | String(20) | CHECK IN (`'accepted'`, `'rejected'`, `'needs_review'`), NOT NULL |
| `actor_id` | String(100) | NOT NULL |
| `role` | String(32) | CHECK IN (`'owner'`, `'member'`), NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- Composite FK `(workspace_id, inventory_id)` → `source_claim_inventories(workspace_id, id)`
- Append-only trigger (no UPDATE/DELETE)

### `localization_claim_decision_sets`

Immutable, reusable snapshot of all Claim decisions for one Inventory in one target market and one frozen Truth/MarketPolicy context. `origin_batch_id` records where the user first made the decisions but is not ownership: another Batch may reuse the set only when its complete canonical identity matches. Referenced by CopyVersion, Item fingerprint, and lineage.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `origin_batch_id` | String(64) | NOT NULL |
| `inventory_id` | String(64) | NOT NULL |
| `source_asset_version_id` | String(64) | NOT NULL — denormalized Inventory source identity |
| `truth_package_id` | String(64) | NOT NULL |
| `truth_package_market_id` | String(64) | NOT NULL |
| `target_market` | String(10) | NOT NULL |
| `market_policy_version_id` | String(64) | NOT NULL |
| `decision_count` | Integer | NOT NULL, CHECK `decision_count >= 0` |
| `canonical_hash` | String(64) | NOT NULL — hash of Inventory, Truth market, MarketPolicy, and the complete ordered decision rows |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, id, inventory_id, truth_package_id, truth_package_market_id, target_market, market_policy_version_id)` — parent-carrying target for decisions, CopyVersions, and Items
- `UNIQUE(workspace_id, id, source_asset_version_id, inventory_id, truth_package_id, truth_package_market_id, target_market, market_policy_version_id)` — complete source-context target for CopyVersions, Items, and Lineage
- `UNIQUE(workspace_id, canonical_hash)` — idempotent reuse
- Composite FK `(workspace_id, origin_batch_id, truth_package_id)` → `localization_batches(workspace_id, id, truth_package_id)` — proves the originating Batch resolved the same TruthPackage
- Composite FK `(workspace_id, inventory_id, source_asset_version_id)` → `source_claim_inventories(workspace_id, id, asset_version_id)`
- Composite FK `(workspace_id, truth_package_market_id, truth_package_id, target_market, market_policy_version_id)` → the corresponding parent-carrying key on `truth_package_markets`
- A PostgreSQL DEFERRABLE INITIALLY DEFERRED constraint trigger requires exactly `decision_count` child rows at commit. `claims.py` verifies the same completeness plus the canonical hash over ordered decisions before reuse or Batch freeze on both databases.
- Immutable trigger

### `localization_claim_decisions`

Individual per-market claim decision rows within a decision set. Immutable.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `decision_set_id` | String(64) | NOT NULL |
| `inventory_id` | String(64) | NOT NULL — denormalized parent identity |
| `claim_index` | Integer | NOT NULL — position within inventory `claims_json` |
| `claim_scope_key` | String(120) | NOT NULL — stable scope copied from the immutable observation |
| `target_market` | String(10) | NOT NULL |
| `truth_package_id` | String(64) | NOT NULL |
| `truth_package_market_id` | String(64) | NOT NULL |
| `market_policy_version_id` | String(64) | NOT NULL |
| `action` | String(20) | CHECK IN (`'replace'`, `'remove'`, `'preserve'`), NOT NULL |
| `replacement_fact_id` | String(64) | nullable — NOT NULL when `action = 'replace'` |
| `replacement_fact_scope_key` | String(120) | nullable — NOT NULL when `action = 'replace'`; must equal `claim_scope_key` |
| `removal_strategy_json` | JSON | nullable — frozen mask/region strategy, NOT NULL when `action = 'remove'` |
| `removal_strategy_hash` | String(64) | nullable — hash of the frozen strategy |
| `preserve_policy_evidence_hash` | String(64) | nullable — proof that the frozen MarketPolicy classifies the Claim as market-neutral |
| `rationale` | String(500) | nullable |
| `actor_id` | String(100) | NOT NULL |
| `role` | String(32) | CHECK IN (`'owner'`, `'member'`), NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, decision_set_id, claim_index)` — one decision per observation in this per-market set
- Composite FK `(workspace_id, decision_set_id, inventory_id, truth_package_id, truth_package_market_id, target_market, market_policy_version_id)` → the corresponding parent-carrying key on `localization_claim_decision_sets`
- Composite FK `(workspace_id, replacement_fact_id, truth_package_id, truth_package_market_id, replacement_fact_scope_key)` → `truth_facts(workspace_id, id, truth_package_id, truth_package_market_id, scope_key)` when `replacement_fact_id` is NOT NULL
- PostgreSQL trigger validates that `claim_index` exists in the set's immutable Inventory and that its stored scope equals `claim_scope_key`; the `claims.py` interface performs the same check on SQLite
- CHECK: `(action = 'replace' AND replacement_fact_id IS NOT NULL AND replacement_fact_scope_key = claim_scope_key AND removal_strategy_json IS NULL AND removal_strategy_hash IS NULL AND preserve_policy_evidence_hash IS NULL) OR (action != 'replace' AND replacement_fact_id IS NULL AND replacement_fact_scope_key IS NULL)`
- CHECK: `(action = 'remove' AND removal_strategy_json IS NOT NULL AND removal_strategy_hash IS NOT NULL AND preserve_policy_evidence_hash IS NULL) OR (action != 'remove' AND removal_strategy_json IS NULL AND removal_strategy_hash IS NULL)`
- CHECK: `(action = 'preserve' AND preserve_policy_evidence_hash IS NOT NULL) OR (action != 'preserve' AND preserve_policy_evidence_hash IS NULL)`
- Immutable trigger

## 1.5 Localization batch tables

### `localization_batches`

The main batch record. Workspace-scoped. Mutable via CAS on `revision`.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `product_version_id` | String(64) | NOT NULL |
| `offer_version_id` | String(64) | NOT NULL |
| `source_market_snapshot_json` | JSON | NOT NULL — frozen source market context |
| `truth_package_id` | String(64) | nullable — set during preparation |
| `truth_data_mode` | String(20) | CHECK IN (`'real'`, `'mock'`), NOT NULL |
| `provider_execution_mode` | String(20) | CHECK IN (`'byok'`, `'sandbox'`, `'mock'`), NOT NULL |
| `evidence_tier` | String(20) | CHECK IN (`'production'`, `'sandbox'`, `'mock'`), nullable — derived at freeze, never caller-supplied |
| `phase` | String(32) | CHECK IN (`'draft'`, `'preparing'`, `'awaiting_copy_approval'`, `'ready'`, `'running'`, `'closed'`, `'cancelled'`), NOT NULL |
| `preparation_stage` | String(20) | CHECK IN (`'truth'`, `'analysis'`, `'claim_review'`, `'copy'`, `'complete'`), nullable — CAS projection within `preparing` phase |
| `outcome` | String(32) | CHECK IN (`'none'`, `'succeeded'`, `'partially_succeeded'`, `'failed'`), NOT NULL, default `'none'` |
| `truth_freshness` | String(20) | CHECK IN (`'current'`, `'superseded'`, `'expired'`), nullable |
| `frozen_recipe_json` | JSON | nullable — set at freeze: edit/QC provider, model, params, rules, per-step scenario snapshot |
| `max_attempts` | Integer | NOT NULL, default 3, CHECK `max_attempts BETWEEN 1 AND 3` |
| `revision` | Integer | NOT NULL, default 1 — CAS |
| `frozen_at` | DateTime(tz) | nullable — set once at confirmation |
| `created_at` | DateTime(tz) | NOT NULL |
| `updated_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, id, truth_package_id)` — frozen Truth target for Items when `truth_package_id` is set
- `UNIQUE(workspace_id, id, truth_package_id, offer_version_id)` — binding context
- `UNIQUE(workspace_id, id, truth_package_id, product_version_id, offer_version_id)` — complete frozen Batch context for Lineage
- `UNIQUE(workspace_id, id, truth_package_id, provider_execution_mode)` — provider-call context
- `UNIQUE(workspace_id, id, truth_package_id, product_version_id, offer_version_id, provider_execution_mode)` — complete Lineage context including localization-provider mode
- `UNIQUE(workspace_id, id, truth_package_id, offer_version_id, evidence_tier)` — complete binding evidence context
- `UNIQUE(workspace_id, id, truth_package_id, product_version_id, offer_version_id, truth_data_mode, provider_execution_mode, evidence_tier)` — complete Lineage provenance context
- Composite FK `(workspace_id, truth_package_id)` → `truth_packages(workspace_id, id)` when NOT NULL
- Composite FK `(workspace_id, product_version_id)` → `library_versions(workspace_id, id)`
- Composite FK `(workspace_id, offer_version_id)` → `library_versions(workspace_id, id)`
- `frozen_at` is write-once: PostgreSQL trigger rejects UPDATE when the existing value is NOT NULL
- After `frozen_at` is set, the following columns are frozen: `product_version_id`, `offer_version_id`, `source_market_snapshot_json`, `truth_package_id`, `truth_data_mode`, `provider_execution_mode`, `evidence_tier`, `frozen_recipe_json`, `max_attempts`. PostgreSQL trigger rejects UPDATE on these columns when `frozen_at IS NOT NULL`. Only `phase`, `preparation_stage`, `outcome`, `truth_freshness`, `revision`, and `updated_at` remain mutable after freeze.
- Source and target membership rows are immutable once inserted. Their triggers reject UPDATE/DELETE, and reject new INSERTs unless the parent Batch is still unfrozen and in `draft`; therefore no membership can be added, removed, or rewritten after confirmation.

### `localization_batch_sources`

Per source AssetVersion in a batch. Multiple sources per batch. Workspace-scoped.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `batch_id` | String(64) | NOT NULL |
| `asset_version_id` | String(64) | NOT NULL |
| `decision_id` | String(64) | NOT NULL — the Winner ExperimentDecision |
| `experiment_id` | String(64) | NOT NULL — denormalized Winner context |
| `experiment_version_id` | String(64) | NOT NULL — exact frozen ExperimentVersion |
| `experiment_version_candidate_id` | String(64) | NOT NULL — exact source candidate |
| `source_generation_mode` | String(20) | CHECK IN (`'byok'`, `'sandbox'`, `'mock'`), NOT NULL — copied from the exact AssetVersion when selected, revalidated at freeze |
| `experiment_evidence_mode` | String(20) | CHECK IN (`'production'`, `'sandbox'`, `'mock'`), NOT NULL — derived from the authoritative decision/confirmation when selected, revalidated at freeze |
| `source_market_snapshot_json` | JSON | NOT NULL — frozen Winner context |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, id, batch_id, asset_version_id)` — preparation-work target
- `UNIQUE(workspace_id, id, batch_id, asset_version_id, decision_id)` — parent-carrying alternate key for child FKs
- `UNIQUE(workspace_id, id, batch_id, asset_version_id, decision_id, source_generation_mode, experiment_evidence_mode)` — complete Lineage provenance target
- `UNIQUE(workspace_id, batch_id, asset_version_id)` — no duplicate source in one batch
- Composite FK `(workspace_id, batch_id)` → `localization_batches(workspace_id, id)`
- Composite FK `(workspace_id, asset_version_id)` → `asset_versions(workspace_id, id)`
- Composite FK `(workspace_id, decision_id, experiment_id, experiment_version_id, experiment_version_candidate_id)` → the existing parent-carrying alternate key on `experiment_decisions`
- Composite FK `(workspace_id, experiment_version_id, experiment_version_candidate_id, asset_version_id)` → `experiment_version_candidates(workspace_id, experiment_version_id, id, asset_version_id)` — proves the Winner candidate is the same source AssetVersion
- Immutable-row trigger rejects UPDATE/DELETE; INSERT additionally requires the parent Batch to be unfrozen and in `draft`

### `localization_batch_commands`

Append-only command-to-Operation linkage. Per §2A, replaces singular `operation_id`.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `batch_id` | String(64) | NOT NULL |
| `item_id` | String(64) | nullable — for Item-scoped commands like `retry_item` |
| `command_kind` | String(40) | NOT NULL — e.g. `'create_draft'`, `'start_preparation'`, `'submit_claim_decisions'`, `'confirm'`, `'retry_item'`, `'authorize_isolated_qc'`, `'adopt_isolated_output'`, `'cancel'` |
| `operation_id` | String(64) | nullable — NULL for commands without external calls |
| `semantic_key_hash` | String(64) | NOT NULL — idempotency key for command replay |
| `request_fingerprint_hash` | String(64) | NOT NULL |
| `actor_id` | String(100) | NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, id, batch_id)` — parent-carrying target for preparation work
- `UNIQUE(workspace_id, id, batch_id, operation_id)` — provider-call target when `operation_id` is set
- `UNIQUE(workspace_id, semantic_key_hash)` — command replay
- `UNIQUE(workspace_id, operation_id)` partial where `operation_id IS NOT NULL`
- Composite FK `(workspace_id, batch_id)` → `localization_batches(workspace_id, id)`
- Composite FK `(workspace_id, item_id, batch_id)` → `localization_items(workspace_id, id, batch_id)` when `item_id` is NOT NULL — added after `localization_items` exists in the same migration
- Composite FK `(workspace_id, operation_id)` → `operation_records(workspace_id, id)` when NOT NULL
- Append-only trigger

### `localization_batch_market_targets`

Which markets a batch targets. Workspace-scoped.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `batch_id` | String(64) | NOT NULL |
| `market_policy_version_id` | String(64) | FK → `market_policy_versions.id`, NOT NULL (platform FK) |
| `market` | String(10) | NOT NULL — denormalized |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, batch_id, market)` — one target per market per batch
- `UNIQUE(workspace_id, id, batch_id, market, market_policy_version_id)` — parent-carrying target for Items
- Composite FK `(workspace_id, batch_id)` → `localization_batches(workspace_id, id)`
- PostgreSQL INSERT trigger requires `market` to equal the `market_policies.market_id` reached through `market_policy_version_id`; `batch.py` enforces the same rule on SQLite
- Immutable-row trigger rejects UPDATE/DELETE; INSERT additionally requires the parent Batch to be unfrozen and in `draft`

## 1.6 Copy and translation tables

### `localized_copy_versions`

Immutable market-ready copy versions. A version may come from external translation or a deterministic source-locale projection that still applies Claim/Fact policy. Supports cross-batch reuse via semantic fingerprint. Workspace-scoped.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `copy_series_hash` | String(64) | NOT NULL — semantic identity: source AV + inventory + target market + truth package + claim decision set + copy policy snapshot + copy origin + translation recipe/provider/mode/scenario when used |
| `version_number` | Integer | NOT NULL |
| `source_asset_version_id` | String(64) | NOT NULL |
| `inventory_id` | String(64) | NOT NULL |
| `truth_package_id` | String(64) | NOT NULL |
| `truth_package_market_id` | String(64) | NOT NULL |
| `claim_decision_set_id` | String(64) | NOT NULL |
| `target_market` | String(10) | NOT NULL |
| `market_policy_version_id` | String(64) | NOT NULL |
| `content_json` | JSON | NOT NULL — translated segments with locked placeholders |
| `copy_origin` | String(32) | CHECK IN (`'translated'`, `'source_locale_projection'`), NOT NULL |
| `translation_execution_mode` | String(20) | CHECK IN (`'byok'`, `'sandbox'`, `'mock'`), nullable — required only for translated copy |
| `translation_provider` | String(64) | nullable — required only for translated copy |
| `translation_model` | String(200) | nullable — required only for translated copy |
| `translation_adapter_version` | String(40) | nullable — required only for translated copy |
| `mock_scenario` | String(100) | nullable |
| `mock_version` | String(40) | nullable |
| `brand_localization_policy_version_id` | String(64) | nullable — FK to library_versions when a workspace policy exists |
| `copy_policy_snapshot_json` | JSON | NOT NULL — full resolved snapshot of platform + workspace policy |
| `copy_policy_snapshot_hash` | String(64) | NOT NULL — hash of `copy_policy_snapshot_json` |
| `state` | String(20) | CHECK IN (`'draft'`, `'needs_review'`, `'approved'`, `'superseded'`), NOT NULL |
| `evidence_hash` | String(64) | NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, id, source_asset_version_id, inventory_id, truth_package_id, truth_package_market_id, claim_decision_set_id, target_market, market_policy_version_id)` — parent-carrying target for Items and Lineage
- `UNIQUE(workspace_id, id, source_asset_version_id, inventory_id, truth_package_id, truth_package_market_id, claim_decision_set_id, target_market, market_policy_version_id, translation_execution_mode)` — translated preparation-result target
- `UNIQUE(workspace_id, copy_series_hash, version_number)` — version scoped by series
- Composite FK `(workspace_id, truth_package_market_id, truth_package_id, target_market, market_policy_version_id)` → the corresponding parent-carrying key on `truth_package_markets`
- Composite FK `(workspace_id, claim_decision_set_id, source_asset_version_id, inventory_id, truth_package_id, truth_package_market_id, target_market, market_policy_version_id)` → the complete source-context key on `localization_claim_decision_sets`
- Composite FK `(workspace_id, source_asset_version_id)` → `asset_versions(workspace_id, id)`
- Composite FK `(workspace_id, inventory_id, source_asset_version_id)` → `source_claim_inventories(workspace_id, id, asset_version_id)`
- Composite FK `(workspace_id, truth_package_id)` → `truth_packages(workspace_id, id)`
- Composite FK `(workspace_id, brand_localization_policy_version_id)` → `library_versions(workspace_id, id)` when NOT NULL
- PostgreSQL trigger verifies any non-null Brand policy version has Library entry type `brand_localization_policy`; the `copy.py` interface performs the same check on SQLite
- CHECK: `(copy_origin = 'translated' AND translation_execution_mode IS NOT NULL AND translation_provider IS NOT NULL AND translation_model IS NOT NULL AND translation_adapter_version IS NOT NULL) OR (copy_origin = 'source_locale_projection' AND translation_execution_mode IS NULL AND translation_provider IS NULL AND translation_model IS NULL AND translation_adapter_version IS NULL)`
- CHECK: `(translation_execution_mode = 'mock' AND mock_scenario IS NOT NULL AND mock_version IS NOT NULL) OR (translation_execution_mode IN ('byok', 'sandbox') AND mock_scenario IS NULL AND mock_version IS NULL) OR (translation_execution_mode IS NULL AND mock_scenario IS NULL AND mock_version IS NULL)`
- Immutable trigger on all columns EXCEPT `state`. Allowed transitions are `draft → needs_review|superseded`, `needs_review → approved|superseded`, and `approved → superseded`; `superseded` is terminal.

### `localized_copy_approvals`

Append-only copy approval records. Workspace-scoped.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `copy_version_id` | String(64) | NOT NULL |
| `actor_id` | String(100) | NOT NULL |
| `role` | String(32) | CHECK IN (`'owner'`, `'member'`), NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- Composite FK `(workspace_id, copy_version_id)` → `localized_copy_versions(workspace_id, id)`
- Append-only trigger

## 1.7 Item and attempt tables

### `localization_items`

One row per source × target market execution unit. Workspace-scoped, mutable via CAS.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `batch_id` | String(64) | NOT NULL |
| `batch_source_id` | String(64) | NOT NULL |
| `batch_market_target_id` | String(64) | NOT NULL |
| `source_asset_version_id` | String(64) | NOT NULL — denormalized source identity |
| `decision_id` | String(64) | NOT NULL — denormalized Winner identity |
| `target_market` | String(10) | NOT NULL |
| `market_policy_version_id` | String(64) | FK → `market_policy_versions.id`, NOT NULL (platform FK) |
| `truth_package_id` | String(64) | NOT NULL |
| `truth_package_market_id` | String(64) | NOT NULL |
| `inventory_id` | String(64) | NOT NULL |
| `claim_decision_set_id` | String(64) | NOT NULL |
| `copy_version_id` | String(64) | nullable — set when copy is planned |
| `state` | String(20) | CHECK IN (`'queued'`, `'running'`, `'retry_wait'`, `'paused'`, `'succeeded'`, `'failed'`, `'skipped'`, `'cancelled'`, `'uncertain'`), NOT NULL |
| `reason_code` | String(60) | nullable — stable machine-readable code |
| `reason_detail` | String(500) | nullable — human-readable explanation |
| `recoverable` | Boolean | nullable — whether the state can be exited |
| `next_action` | String(60) | nullable — suggested recovery action |
| `attempt_count` | Integer | NOT NULL, default 0 |
| `item_fingerprint_hash` | String(64) | NOT NULL — semantic fingerprint: source AV + decision + offer + market + truth + claim decisions + copy + policy + recipe + modes |
| `revision` | Integer | NOT NULL, default 1 — CAS |
| `created_at` | DateTime(tz) | NOT NULL |
| `updated_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, id, batch_id)` — parent-carrying alternate key for child FKs
- `UNIQUE(workspace_id, id, batch_id, truth_package_id)` — provider-call target
- `UNIQUE(workspace_id, id, batch_id, source_asset_version_id, target_market, market_policy_version_id, truth_package_id)` — complete binding context
- `UNIQUE(workspace_id, id, batch_id, batch_source_id, source_asset_version_id, decision_id, target_market, market_policy_version_id, truth_package_id, truth_package_market_id, inventory_id, claim_decision_set_id, copy_version_id)` — complete localized-Lineage target when `copy_version_id` is set
- `UNIQUE(workspace_id, batch_id, batch_source_id, target_market)` — one item per source × market
- Composite FK `(workspace_id, batch_source_id, batch_id, source_asset_version_id, decision_id)` → `localization_batch_sources(workspace_id, id, batch_id, asset_version_id, decision_id)`
- Composite FK `(workspace_id, batch_market_target_id, batch_id, target_market, market_policy_version_id)` → `localization_batch_market_targets(workspace_id, id, batch_id, market, market_policy_version_id)`
- Composite FK `(workspace_id, batch_id, truth_package_id)` → `localization_batches(workspace_id, id, truth_package_id)`
- Composite FK `(workspace_id, truth_package_market_id, truth_package_id, target_market, market_policy_version_id)` → the corresponding parent-carrying key on `truth_package_markets`
- Composite FK `(workspace_id, claim_decision_set_id, source_asset_version_id, inventory_id, truth_package_id, truth_package_market_id, target_market, market_policy_version_id)` → the complete source-context key on `localization_claim_decision_sets`
- Composite FK `(workspace_id, copy_version_id, source_asset_version_id, inventory_id, truth_package_id, truth_package_market_id, claim_decision_set_id, target_market, market_policy_version_id)` → the corresponding parent-carrying key on `localized_copy_versions` when `copy_version_id` is NOT NULL

### `localization_attempts`

Immutable header per attempt. Workspace-scoped. At most 3 per item.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `item_id` | String(64) | NOT NULL |
| `batch_id` | String(64) | NOT NULL — denormalized parent identity |
| `attempt_number` | Integer | NOT NULL, CHECK `attempt_number BETWEEN 1 AND 3` |
| `provider` | String(64) | NOT NULL |
| `model` | String(200) | NOT NULL |
| `prompt_params_hash` | String(64) | NOT NULL |
| `input_versions_hash` | String(64) | NOT NULL |
| `state` | String(20) | CHECK IN (`'authorized'`, `'dispatching'`, `'completed'`, `'failed'`, `'uncertain'`), NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, id, item_id, batch_id)` — parent-carrying alternate key for child FKs
- `UNIQUE(workspace_id, item_id, attempt_number)` — one attempt per number per item
- Composite FK `(workspace_id, item_id, batch_id)` → `localization_items(workspace_id, id, batch_id)` — proves attempt's item belongs to same batch context
- Immutable trigger on all columns EXCEPT `state`, which permits `authorized → dispatching`, `dispatching → completed|failed|uncertain`, and evidence-backed reconciliation `uncertain → completed|failed`. The reconciliation command must append ProviderAttempt evidence in the same transaction; PostgreSQL trigger enforces the transition graph and rejects every other edge.

### `localization_attempt_outputs`

Immutable output record. No `qc_passed` field per §2A. Workspace-scoped. At most one output per attempt.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `attempt_id` | String(64) | NOT NULL |
| `item_id` | String(64) | NOT NULL — denormalized for parent-carrying FK |
| `batch_id` | String(64) | NOT NULL — denormalized parent identity |
| `object_key` | String(1000) | NOT NULL |
| `content_sha256` | String(64) | NOT NULL |
| `byte_size` | Integer | NOT NULL |
| `mime_type` | String(100) | NOT NULL |
| `output_evidence_hash` | String(64) | NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, id, attempt_id, item_id, batch_id)` — publication and Lineage target
- `UNIQUE(workspace_id, attempt_id)` — at most one output per attempt
- Composite FK `(workspace_id, attempt_id, item_id, batch_id)` → `localization_attempts(workspace_id, id, item_id, batch_id)`
- Immutable trigger. “Isolated” is derived by the absence of a `localization_output_publications` row; publication creates a separate immutable record and never flips a flag on the output.

### `localization_output_publications`

Records the single publication event for an attempt output, whether from normal finalization or explicit adoption. Replaces the previous `localization_attempt_output_adoptions` table with a unified record that guarantees single-use at the database level for both paths.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `output_id` | String(64) | NOT NULL |
| `attempt_id` | String(64) | NOT NULL |
| `item_id` | String(64) | NOT NULL |
| `batch_id` | String(64) | NOT NULL |
| `publication_kind` | String(20) | CHECK IN (`'normal'`, `'adopted'`), NOT NULL |
| `asset_version_id` | String(64) | NOT NULL — the created localized AssetVersion |
| `actor_id` | String(100) | NOT NULL |
| `role` | String(32) | CHECK IN (`'owner'`, `'member'`), NOT NULL |
| `recheck_hash` | String(64) | nullable — NOT NULL when `publication_kind = 'adopted'` |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, output_id)` — single use, regardless of publication kind
- `UNIQUE(workspace_id, asset_version_id)` — one publication per AssetVersion
- `UNIQUE(workspace_id, id, asset_version_id, item_id, batch_id)` — binding target
- `UNIQUE(workspace_id, asset_version_id, output_id, attempt_id, item_id, batch_id)` — complete Lineage target
- `UNIQUE(workspace_id, id, asset_version_id, output_id, attempt_id, item_id, batch_id)` — publication-identity Lineage target
- Composite FK `(workspace_id, output_id, attempt_id, item_id, batch_id)` → `localization_attempt_outputs(workspace_id, id, attempt_id, item_id, batch_id)`
- Composite FK `(workspace_id, asset_version_id)` → `asset_versions(workspace_id, id)`
- CHECK: `(publication_kind = 'adopted' AND recheck_hash IS NOT NULL) OR (publication_kind = 'normal' AND recheck_hash IS NULL)`
- Immutable trigger

## 1.8 QC tables

### `localization_qc_records`

Append-only per-check records for each attempt. Workspace-scoped.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `attempt_id` | String(64) | NOT NULL |
| `item_id` | String(64) | NOT NULL |
| `batch_id` | String(64) | NOT NULL |
| `check_kind` | String(40) | NOT NULL — e.g. `'locked_fact'`, `'number_match'`, `'currency_match'`, `'claim_decision'`, `'placeholder_count'`, `'forbidden_term'`, `'new_commercial_claim'`, `'ocr'`, `'layout'`, `'visual_fidelity'` |
| `check_slot_key` | String(120) | NOT NULL — stable frozen-plan identity, including scope/region when a kind repeats |
| `expected_json` | JSON | NOT NULL |
| `observed_json` | JSON | NOT NULL |
| `evidence_location_json` | JSON | nullable — region coordinates, OCR span, etc. |
| `severity` | String(20) | CHECK IN (`'blocking'`, `'warning'`), NOT NULL |
| `result` | String(10) | CHECK IN (`'pass'`, `'fail'`), NOT NULL |
| `rule_version` | String(40) | NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, attempt_id, check_slot_key)` — idempotent fold yields one authoritative result per frozen check slot
- Composite FK `(workspace_id, attempt_id, item_id, batch_id)` → `localization_attempts(workspace_id, id, item_id, batch_id)`
- Append-only trigger

## 1.9 Provider call join tables

### `localization_item_provider_calls`

Links item-phase localization attempts to `provider_call_attempts` (edit, OCR, visual QC). Workspace-scoped.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `item_id` | String(64) | NOT NULL |
| `attempt_id` | String(64) | NOT NULL — localization attempt |
| `batch_id` | String(64) | NOT NULL |
| `batch_command_id` | String(64) | NOT NULL — confirm/retry/adoption-QC authorization |
| `operation_id` | String(64) | NOT NULL |
| `truth_package_id` | String(64) | NOT NULL |
| `provider_execution_mode` | String(20) | CHECK IN (`'byok'`, `'sandbox'`, `'mock'`), NOT NULL |
| `provider_step_kind` | String(40) | CHECK IN (`'localization_edit'`, `'localization_ocr'`, `'localization_visual_qc'`), NOT NULL |
| `provider_call_attempt_id` | String(64) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, provider_call_attempt_id)` — each provider call linked to at most one localization attempt
- Composite FK `(workspace_id, attempt_id, item_id, batch_id)` → `localization_attempts(workspace_id, id, item_id, batch_id)`
- Composite FK `(workspace_id, item_id, batch_id, truth_package_id)` → `localization_items(workspace_id, id, batch_id, truth_package_id)`
- Composite FK `(workspace_id, batch_id, truth_package_id, provider_execution_mode)` → `localization_batches(workspace_id, id, truth_package_id, provider_execution_mode)`
- Composite FK `(workspace_id, batch_command_id, batch_id, operation_id)` → `localization_batch_commands(workspace_id, id, batch_id, operation_id)`
- Composite FK `(workspace_id, provider_call_attempt_id, operation_id, truth_package_id, provider_step_kind, provider_execution_mode)` → `provider_call_attempts(workspace_id, id, operation_id, truth_package_id, step_kind, execution_mode)`

### `localization_preparation_provider_calls`

Persistent preparation work and phase-1 ProviderAttempt link for analysis or translation. The row exists before dispatch and is the domain subject used by `ExternalCallEvidenceRecorder`; the immutable Inventory or CopyVersion does not need to exist yet. Completion creates the result row and fills the corresponding write-once result ID in the same transaction as ProviderAttempt success evidence.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `batch_id` | String(64) | NOT NULL |
| `batch_source_id` | String(64) | NOT NULL |
| `source_asset_version_id` | String(64) | NOT NULL |
| `batch_command_id` | String(64) | NOT NULL |
| `operation_id` | String(64) | NOT NULL |
| `truth_package_id` | String(64) | NOT NULL |
| `provider_execution_mode` | String(20) | CHECK IN (`'byok'`, `'sandbox'`, `'mock'`), NOT NULL |
| `work_kind` | String(20) | CHECK IN (`'analysis'`, `'translation'`), NOT NULL |
| `provider_step_kind` | String(40) | CHECK IN (`'source_analysis'`, `'translation'`), NOT NULL |
| `input_inventory_id` | String(64) | nullable — required translation input |
| `truth_package_market_id` | String(64) | nullable — required for translation |
| `target_market` | String(10) | nullable — required for translation |
| `market_policy_version_id` | String(64) | nullable — required for translation |
| `claim_decision_set_id` | String(64) | nullable — required for translation |
| `subject_semantic_key_hash` | String(64) | NOT NULL — complete Inventory or Copy work identity known before dispatch |
| `provider_call_attempt_id` | String(64) | NOT NULL |
| `result_inventory_id` | String(64) | nullable — write-once result for analysis |
| `result_copy_version_id` | String(64) | nullable — write-once result for translation |
| `state` | String(20) | CHECK IN (`'prepared'`, `'succeeded'`, `'failed'`, `'uncertain'`), NOT NULL |
| `revision` | Integer | NOT NULL, default 1 — CAS |
| `created_at` | DateTime(tz) | NOT NULL |
| `updated_at` | DateTime(tz) | NOT NULL |
| `completed_at` | DateTime(tz) | nullable |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, subject_semantic_key_hash)` — concurrent preparation cannot duplicate the same external work
- `UNIQUE(workspace_id, provider_call_attempt_id)` — each provider call linked to at most one preparation subject
- Composite FK `(workspace_id, batch_id, truth_package_id, provider_execution_mode)` → `localization_batches(workspace_id, id, truth_package_id, provider_execution_mode)`
- Composite FK `(workspace_id, batch_source_id, batch_id, source_asset_version_id)` → `localization_batch_sources(workspace_id, id, batch_id, asset_version_id)`
- Composite FK `(workspace_id, batch_command_id, batch_id, operation_id)` → `localization_batch_commands(workspace_id, id, batch_id, operation_id)`
- Composite FK `(workspace_id, input_inventory_id, source_asset_version_id)` → `source_claim_inventories(workspace_id, id, asset_version_id)` when `input_inventory_id` is NOT NULL
- Composite FK `(workspace_id, result_inventory_id, source_asset_version_id, provider_execution_mode)` → `source_claim_inventories(workspace_id, id, asset_version_id, analysis_execution_mode)` when `result_inventory_id` is NOT NULL
- Composite FK `(workspace_id, truth_package_market_id, truth_package_id, target_market, market_policy_version_id)` → the corresponding parent-carrying key on `truth_package_markets` when `truth_package_market_id` is NOT NULL
- Composite FK `(workspace_id, claim_decision_set_id, source_asset_version_id, input_inventory_id, truth_package_id, truth_package_market_id, target_market, market_policy_version_id)` → the complete source-context key on `localization_claim_decision_sets` when `claim_decision_set_id` is NOT NULL
- Composite FK `(workspace_id, result_copy_version_id, source_asset_version_id, input_inventory_id, truth_package_id, truth_package_market_id, claim_decision_set_id, target_market, market_policy_version_id, provider_execution_mode)` → the translated preparation-result key on `localized_copy_versions` when `result_copy_version_id` is NOT NULL
- Composite FK `(workspace_id, provider_call_attempt_id, operation_id, truth_package_id, provider_step_kind, provider_execution_mode)` → `provider_call_attempts(workspace_id, id, operation_id, truth_package_id, step_kind, execution_mode)`
- CHECK: `(work_kind = 'analysis' AND provider_step_kind = 'source_analysis' AND input_inventory_id IS NULL AND truth_package_market_id IS NULL AND target_market IS NULL AND market_policy_version_id IS NULL AND claim_decision_set_id IS NULL) OR (work_kind = 'translation' AND provider_step_kind = 'translation' AND input_inventory_id IS NOT NULL AND truth_package_market_id IS NOT NULL AND target_market IS NOT NULL AND market_policy_version_id IS NOT NULL AND claim_decision_set_id IS NOT NULL)`
- CHECK: `(state = 'succeeded' AND completed_at IS NOT NULL AND ((work_kind = 'analysis' AND result_inventory_id IS NOT NULL AND result_copy_version_id IS NULL) OR (work_kind = 'translation' AND result_copy_version_id IS NOT NULL AND result_inventory_id IS NULL))) OR (state != 'succeeded' AND result_inventory_id IS NULL AND result_copy_version_id IS NULL)`
- Trigger makes `work_kind`, semantic key, ProviderAttempt, and parent identities immutable; result IDs are write-once. Allowed state transitions are `prepared → succeeded|failed|uncertain` and evidence-backed reconciliation `uncertain → succeeded|failed`.

### `localization_semantic_claims`

Durable workspace-level canonical claim that prevents concurrent or later Batches from duplicating paid work on the same semantic intent. Created atomically at Item admission and never released or deleted. Identical requests reuse its canonical Item/Operation/result or return its terminal failure; explicit RetryItem updates the same canonical Item.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `item_fingerprint_hash` | String(64) | NOT NULL |
| `batch_id` | String(64) | NOT NULL |
| `item_id` | String(64) | NOT NULL |
| `current_operation_id` | String(64) | nullable — current authorized Operation while active/retrying |
| `claim_state` | String(20) | CHECK IN (`'active'`, `'succeeded'`, `'failed'`, `'cancelled'`), NOT NULL |
| `revision` | Integer | NOT NULL, default 1 — CAS |
| `created_at` | DateTime(tz) | NOT NULL |
| `updated_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, item_fingerprint_hash)` — one canonical claim for the fingerprint across every lifecycle state
- Composite FK `(workspace_id, batch_id)` → `localization_batches(workspace_id, id)`
- Composite FK `(workspace_id, item_id, batch_id)` → `localization_items(workspace_id, id, batch_id)`
- Composite FK `(workspace_id, current_operation_id)` → `operation_records(workspace_id, id)` when NOT NULL
- CAS trigger permits `active → succeeded|failed|cancelled` and `failed → active` only when an explicit RetryItem command links a new `current_operation_id` in the same transaction. `succeeded` and `cancelled` are terminal. The trigger makes the canonical Item/fingerprint immutable and prevents DELETE.

## 1.10 Binding and lineage tables

### `market_asset_bindings`

Persistent relationship between a source AssetVersion and its target-market representation. Workspace-scoped, mutable via CAS.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `source_asset_version_id` | String(64) | NOT NULL |
| `target_market` | String(10) | NOT NULL |
| `offer_version_id` | String(64) | NOT NULL |
| `bound_asset_version_id` | String(64) | NOT NULL — the localized or reused version |
| `binding_kind` | String(20) | CHECK IN (`'localized'`, `'source_reuse'`), NOT NULL |
| `publication_id` | String(64) | nullable — required for a localized child, absent for source reuse |
| `review_state` | String(20) | CHECK IN (`'pending_review'`, `'approved'`, `'rejected'`, `'needs_changes'`), NOT NULL |
| `truth_freshness` | String(20) | CHECK IN (`'current'`, `'stale'`), NOT NULL |
| `ready_for_experiment` | Boolean | NOT NULL, default false — derived from review_state + truth_freshness + upstream checks |
| `active` | Boolean | NOT NULL, default true |
| `batch_id` | String(64) | NOT NULL — provenance |
| `item_id` | String(64) | NOT NULL — provenance |
| `truth_package_id` | String(64) | NOT NULL |
| `market_policy_version_id` | String(64) | NOT NULL |
| `evidence_tier` | String(20) | CHECK IN (`'production'`, `'sandbox'`, `'mock'`), NOT NULL |
| `revision` | Integer | NOT NULL, default 1 — CAS |
| `created_at` | DateTime(tz) | NOT NULL |
| `updated_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- Partial unique: `UNIQUE(workspace_id, source_asset_version_id, target_market, offer_version_id) WHERE active = true` — one active binding per (source, market, offer)
- Partial unique: `UNIQUE(workspace_id, publication_id) WHERE publication_id IS NOT NULL` — one localized binding per publication
- Composite FK `(workspace_id, item_id, batch_id, source_asset_version_id, target_market, market_policy_version_id, truth_package_id)` → `localization_items(workspace_id, id, batch_id, source_asset_version_id, target_market, market_policy_version_id, truth_package_id)`
- Composite FK `(workspace_id, publication_id, bound_asset_version_id, item_id, batch_id)` → the corresponding parent-carrying key on `localization_output_publications` when `publication_id` is NOT NULL
- Composite FK `(workspace_id, source_asset_version_id)` → `asset_versions(workspace_id, id)`
- Composite FK `(workspace_id, bound_asset_version_id)` → `asset_versions(workspace_id, id)`
- Composite FK `(workspace_id, batch_id, truth_package_id, offer_version_id, evidence_tier)` → `localization_batches(workspace_id, id, truth_package_id, offer_version_id, evidence_tier)`
- Composite FK `(workspace_id, truth_package_id)` → `truth_packages(workspace_id, id)`
- Composite FK `(workspace_id, offer_version_id)` → `library_versions(workspace_id, id)`
- CHECK: `(binding_kind = 'localized' AND publication_id IS NOT NULL) OR (binding_kind = 'source_reuse' AND publication_id IS NULL AND bound_asset_version_id = source_asset_version_id)`
- CAS updates may change only review/freshness/readiness/activity projection fields. Every provenance field is immutable.

### `market_asset_binding_events`

Append-only lifecycle events for bindings. Workspace-scoped.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `binding_id` | String(64) | NOT NULL |
| `sequence` | Integer | NOT NULL, CHECK `sequence >= 1` |
| `event_type` | String(40) | NOT NULL — e.g. `'created'`, `'review_changed'`, `'truth_stale'`, `'truth_refreshed'`, `'superseded'`, `'eligibility_changed'` |
| `dependency_change_id` | String(64) | nullable — for deduplication of stale propagation events |
| `previous_state_json` | JSON | NOT NULL |
| `new_state_json` | JSON | NOT NULL |
| `actor_id` | String(100) | NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, binding_id, sequence)` — deterministic incremental fold and projection rebuild order
- `UNIQUE(workspace_id, binding_id, dependency_change_id)` partial where `dependency_change_id IS NOT NULL` — deduplication
- Composite FK `(workspace_id, binding_id)` → `market_asset_bindings(workspace_id, id)`
- Append-only trigger

### `localized_asset_lineages`

Immutable provenance record for every published localized AssetVersion. Workspace-scoped.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | FK → `workspaces.id`, NOT NULL |
| `asset_version_id` | String(64) | NOT NULL — the localized child |
| `source_asset_version_id` | String(64) | NOT NULL |
| `batch_id` | String(64) | NOT NULL |
| `batch_source_id` | String(64) | NOT NULL |
| `item_id` | String(64) | NOT NULL |
| `attempt_id` | String(64) | NOT NULL |
| `output_id` | String(64) | NOT NULL |
| `publication_id` | String(64) | NOT NULL |
| `decision_id` | String(64) | NOT NULL — Winner ExperimentDecision |
| `claim_decision_set_id` | String(64) | NOT NULL — per-market claim decisions applied |
| `product_version_id` | String(64) | NOT NULL |
| `offer_version_id` | String(64) | NOT NULL |
| `source_market` | String(10) | NOT NULL |
| `target_market` | String(10) | NOT NULL |
| `truth_package_id` | String(64) | NOT NULL |
| `truth_package_market_id` | String(64) | NOT NULL |
| `inventory_id` | String(64) | NOT NULL |
| `copy_version_id` | String(64) | NOT NULL |
| `market_policy_version_id` | String(64) | NOT NULL |
| `source_generation_mode` | String(20) | NOT NULL |
| `experiment_evidence_mode` | String(20) | NOT NULL |
| `truth_data_mode` | String(20) | NOT NULL |
| `provider_execution_mode` | String(20) | NOT NULL |
| `evidence_tier` | String(20) | NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL |

Constraints:
- `UNIQUE(workspace_id, id)`
- `UNIQUE(workspace_id, asset_version_id)` — one lineage per localized AssetVersion
- Composite FK `(workspace_id, batch_id, truth_package_id, product_version_id, offer_version_id, truth_data_mode, provider_execution_mode, evidence_tier)` → `localization_batches(workspace_id, id, truth_package_id, product_version_id, offer_version_id, truth_data_mode, provider_execution_mode, evidence_tier)`
- Composite FK `(workspace_id, batch_source_id, batch_id, source_asset_version_id, decision_id, source_generation_mode, experiment_evidence_mode)` → `localization_batch_sources(workspace_id, id, batch_id, asset_version_id, decision_id, source_generation_mode, experiment_evidence_mode)`
- Composite FK `(workspace_id, item_id, batch_id, batch_source_id, source_asset_version_id, decision_id, target_market, market_policy_version_id, truth_package_id, truth_package_market_id, inventory_id, claim_decision_set_id, copy_version_id)` → the complete localized-Lineage key on `localization_items`
- Composite FK `(workspace_id, attempt_id, item_id, batch_id)` → `localization_attempts(workspace_id, id, item_id, batch_id)`
- Composite FK `(workspace_id, output_id, attempt_id, item_id, batch_id)` → `localization_attempt_outputs(workspace_id, id, attempt_id, item_id, batch_id)`
- Composite FK `(workspace_id, publication_id, asset_version_id, output_id, attempt_id, item_id, batch_id)` → `localization_output_publications(workspace_id, id, asset_version_id, output_id, attempt_id, item_id, batch_id)`
- Composite FK `(workspace_id, truth_package_market_id, truth_package_id, target_market, market_policy_version_id)` → the corresponding parent-carrying key on `truth_package_markets`
- Composite FK `(workspace_id, claim_decision_set_id, source_asset_version_id, inventory_id, truth_package_id, truth_package_market_id, target_market, market_policy_version_id)` → the complete source-context key on `localization_claim_decision_sets`
- Composite FK `(workspace_id, copy_version_id, source_asset_version_id, inventory_id, truth_package_id, truth_package_market_id, claim_decision_set_id, target_market, market_policy_version_id)` → the corresponding parent-carrying key on `localized_copy_versions`
- Composite FK `(workspace_id, asset_version_id, truth_package_id, source_asset_version_id)` → `asset_versions(workspace_id, id, truth_package_id, parent_version_id)` — proves the localized child's Truth and parent match Lineage
- Composite FK `(workspace_id, source_asset_version_id)` → `asset_versions(workspace_id, id)`
- Composite FK `(workspace_id, decision_id)` → `experiment_decisions(workspace_id, id)`
- Composite FK `(workspace_id, truth_package_id, truth_data_mode)` → `truth_packages(workspace_id, id, truth_data_mode)`
- Composite FK `(workspace_id, inventory_id, source_asset_version_id)` → `source_claim_inventories(workspace_id, id, asset_version_id)`
- Composite FK `(workspace_id, product_version_id)` and `(workspace_id, offer_version_id)` → `library_versions(workspace_id, id)`; these direct tenant-safe identity checks remain in addition to the parent-carrying FKs above.
- Immutable trigger

## 1.11 Existing table modifications

### `asset_versions`

Evolution:
- `origin_kind` CHECK constraint gains `'localized'`: becomes `IN ('generation', 'upload', 'reuse', 'legacy', 'localized')`
- No new columns are required. `truth_package_id` remains nullable and `parent_version_id` already supports localized → source lineage.
- Add alternate key `UNIQUE(workspace_id, id, truth_package_id, parent_version_id)` for the localized-Lineage FK.
- Add tenant-safe FK `(workspace_id, truth_package_id)` → `truth_packages(workspace_id, id)` when `truth_package_id` is NOT NULL. Before adding it, a migration preflight aborts with the offending IDs if any existing non-null value has no same-Workspace TruthPackage; it never silently nulls or rewrites provenance.
- Add CHECK: `origin_kind != 'localized' OR (truth_package_id IS NOT NULL AND parent_version_id IS NOT NULL)`. Publication creates the AssetVersion, OutputPublication, Lineage, and Binding atomically, so no localized child can commit without both truth and parent provenance.
- After the new publication/lineage/binding tables exist, add a PostgreSQL DEFERRABLE INITIALLY DEFERRED constraint trigger requiring each newly inserted `origin_kind='localized'` row to have exactly one same-Workspace `localization_output_publications` row, one `localized_asset_lineages` row, and its `binding_kind='localized'` MarketAssetBinding by commit. The publication service enforces the same atomic aggregate on SQLite.

### `provider_call_attempts`

Evolution:
- `step_kind` column type widens from `String(20)` to `String(40)` — existing values (`'generation'`, `'visual_qc'`) remain valid; new values (`'source_analysis'`, `'translation'`, `'localization_edit'`, `'localization_ocr'`, `'localization_visual_qc'`) exceed 20 characters when scoped. CHECK constraint becomes `IN ('generation', 'visual_qc', 'source_analysis', 'translation', 'localization_edit', 'localization_ocr', 'localization_visual_qc')`.
- `spend_reservation_id` becomes nullable — NULL for canonical Mock and no-charge Sandbox calls. Chargeable calls still require exactly one SpendReservation.
- New column `chargeability`: `String(24) CHECK IN ('charged', 'free_mock', 'free_sandbox', 'legacy_reserved') NOT NULL` — records why a call was or was not charged. Backfill existing BYOK rows as `charged`; backfill existing Mock/Sandbox rows that already carry a reservation as `legacy_reserved` so migration preserves their historical accounting linkage.
- Add relational CHECK: `(chargeability = 'charged' AND execution_mode = 'byok' AND spend_reservation_id IS NOT NULL) OR (chargeability = 'free_mock' AND execution_mode = 'mock' AND spend_reservation_id IS NULL) OR (chargeability = 'free_sandbox' AND execution_mode = 'sandbox' AND spend_reservation_id IS NULL) OR (chargeability = 'legacy_reserved' AND execution_mode IN ('mock', 'sandbox') AND spend_reservation_id IS NOT NULL AND step_kind IN ('generation', 'visual_qc'))`. A PostgreSQL INSERT trigger and the shared creation interface reject new `legacy_reserved` rows; it is a read-only migration compatibility value. Retry is a new authorized call, not a chargeability class.
- New column `truth_package_id`: `String(64), nullable` — FK → `truth_packages.id`. Composite FK `(workspace_id, truth_package_id)` → `truth_packages(workspace_id, id)`. NULL for generation/visual_qc calls that predate localization; NOT NULL for localization provider calls that depend on truth data.
- Add alternate key `UNIQUE(workspace_id, id, operation_id, truth_package_id, step_kind, execution_mode)` for the two Localization provider-call join tables.
- Add CHECK: `step_kind NOT IN ('source_analysis', 'translation', 'localization_edit', 'localization_ocr', 'localization_visual_qc') OR truth_package_id IS NOT NULL`. Preparation resolves/reuses Truth before analysis admission, so every Localization provider call is tied to the frozen package; legacy generation and generation visual-QC calls remain nullable.
- `run_item_id` remains nullable — localization uses `localization_item_provider_calls` and `localization_preparation_provider_calls` join tables instead of direct FK.

The existing-table alterations occur after `truth_packages` is created and before either localization provider-call join table. This is DDL order inside the issue's single Alembic revision, not an Alembic `depends_on` relationship between tables.

### `library_versions` and `library/contracts.py`

No schema changes are needed for `library_versions`. The existing entry types `data_source`, `manual_fact_set`, `truth_resolution_policy`, and `market_source_mapping` already support their required content. Brand policy is a complete Library contract addition, not only a string constant:

- add `ENTRY_BRAND_LOCALIZATION_POLICY = "brand_localization_policy"` to `ENTRY_TYPES` and to every entry-type response/manifest registry;
- add a closed content validator for claim switches, style guide fields, protected terminology, prohibited phrases, and optional market overrides; reject unknown or malformed policy fields with stable finding codes;
- add canonical hashing and dependency extraction for every referenced Library version, using the existing immutable-version/dependency-index lifecycle;
- include the type in create/edit/fork/clone, validation, publication, withdrawal, export/redaction, and permission dispatch. Member may draft, but only Owner with the existing MFA step-up and confirmation may publish or withdraw;
- activate `WITHDRAWAL_DOWNSTREAM_EVENT_TYPES` for `localization_batch`, `truth_package`, and `market_asset_binding`, and cover the new type in the shared Library contract, authorization, manifest, and lifecycle test suites.

`BrandLocalizationPolicyVersion` content stores per-brand claim/copy rules and is versioned through the standard Library immutable-version lifecycle. `localized_copy_versions.brand_localization_policy_version_id` pins the exact effective version used.

### Migration DDL order

Issue #24 uses one Alembic revision rooted at the repository's actual head when implementation begins. Its `upgrade()` orders DDL explicitly:

1. create MarketPolicy tables and `market_policy_version_events`;
2. create TruthPackage/Fact tables and both typed dependency tables;
3. preflight and alter `asset_versions`, then alter `provider_call_attempts`;
4. create Batch core, source/target membership, and Batch commands, deferring the command → Item FK;
5. create Inventories/confirmations, ClaimDecisionSets/decisions, and CopyVersions/approvals;
6. create Items, then add the deferred command → Item FK; create Attempts, outputs, publications, QC records, semantic claims, and both provider-call join tables;
7. create bindings, binding events, and localized lineage;
8. add PostgreSQL immutability/CAS/type-consistency triggers, RLS/FORCE RLS policies, grants, and metadata assertions after all FK targets exist.

`downgrade()` reverses this dependency order. Any composite FK whose child must be created earlier is added with `op.create_foreign_key()` only after the target alternate key exists; table ordering is never expressed with `depends_on`.

## 1.12 Evidence-tier derivation formula

The Batch's `evidence_tier` is computed at freeze time from all contributing provenance:

```
evidence_tier = min(
    min(tier_of(source.source_generation_mode) for source in batch.sources),
    min(tier_of(source.experiment_evidence_mode) for source in batch.sources),
    tier_of(truth_package.truth_data_mode),
    tier_of(batch.provider_execution_mode),
)
```

Mode normalization:
- `byok`, `real`, `production` → `production`
- `sandbox` → `sandbox`
- `mock` → `mock`

Ordering: `production > sandbox > mock`. The tier takes the minimum (most restrictive) across all dimensions.

A TruthPackage containing both real and mock DataSources is rejected at resolution time — not allowed and then downgraded. A downstream record may retain or lower a tier but may never promote it.

---

# Section 2 (Final): Modules, Adapter Seams, and Worker Pipeline

This section supersedes the revised Section 2. It also makes the small Section 1 corrections listed in §2A; those corrections are required for the interfaces below to be implementable without overloading one `execution_mode`, one `operation_id`, or an output-level `qc_passed` flag.

## 2A. Required corrections to Section 1

### Preserve three provenance dimensions

The design must retain these dimensions independently:

1. **Source provenance** — each selected source `AssetVersion` already has a generation execution mode: `byok | sandbox | mock`. The confirmed experiment candidate/decision contributes its own frozen evidence mode.
2. **Truth provenance** — the Batch selects `truth_data_mode: real | mock`; every selected `DataSourceVersion`, `MarketSourceMappingVersion`, and resulting Fact must match it.
3. **Localization-provider provenance** — the Batch selects `provider_execution_mode: byok | sandbox | mock`; analysis, translation, image edit, OCR when external, and visual QC record the exact mode, adapter version, provider, model, and scenario used by each call.

The Batch also stores a derived `evidence_tier: production | sandbox | mock`. It is computed from all contributing provenance and is never caller supplied:

- any Mock source, experiment evidence, Truth dependency, or localization call makes the result `mock`;
- otherwise, any Sandbox dependency makes the result `sandbox`;
- only entirely production provenance is `production`.

A downstream record may retain or lower this tier, but may never promote it. Exact contributing modes remain in lineage even when the derived tier is convenient for filtering.

The implementation normalizes the heterogeneous mode enums before folding them. `byok`, `real`, and `production` map to `production`; `sandbox` maps to `sandbox`; `mock` maps to `mock`. The Batch tier is the minimum across every selected source AssetVersion mode, every selected source's experiment evidence mode, the TruthPackage data mode, and the localization provider mode. These raw strings are never compared directly.

Apply these schema changes to Section 1:

- `truth_packages.execution_mode` becomes `truth_data_mode IN (real, mock)`.
- `localization_batches.execution_mode` becomes `truth_data_mode`, `provider_execution_mode`, `evidence_tier`, and a frozen per-step provider recipe/scenario snapshot.
- `localization_batch_sources` stores the source AssetVersion mode and confirmed experiment evidence mode/tier copied from authoritative records at selection and revalidates those exact records at freeze time.
- `localized_copy_versions` distinguishes externally translated copy from a deterministic source-locale projection; translated versions store exact provider/model/adapter/mode and Mock scenario/version, and all of these fields enter the semantic fingerprint.
- `localized_asset_lineages` retains source, truth, and every localization call mode rather than collapsing them into one mode.

`list_eligible_winners()` therefore never filters by DataSource mode. DataSource mode is unrelated to Winner production. Winner discovery may filter by the requested Batch evidence tier; final validation derives the source tier from the AssetVersion, frozen experiment candidate, authoritative decision, and confirmation.

### Record every command-to-Operation relationship

Remove the singular `localization_batches.operation_id`. Add an append-only `localization_batch_commands` table with:

- `id`, `workspace_id`, `batch_id`, optional `item_id`, `command_kind`, `operation_id` nullable, `semantic_key_hash`, `request_fingerprint_hash`, `actor_id`, `created_at`;
- `UNIQUE(workspace_id, semantic_key_hash)` for command replay;
- `UNIQUE(workspace_id, operation_id)` where `operation_id IS NOT NULL`;
- tenant-safe composite foreign keys to Batch, Item, and Operation.

Commands that make no external call, such as recording Claim decisions or cancelling a Batch, still receive a command row but do not fabricate an Operation.

Add a small `preparation_stage` CAS projection to `localization_batches`: `truth | analysis | claim_review | copy | complete`. The accepted `BatchPhase` enum remains unchanged; while users resolve Claims, the phase remains `preparing` and this projection explains what is pending.

### Correct AttemptOutput semantics

Remove `qc_passed` from `localization_attempt_outputs`. An output exists before QC and is always immutable. Its eligibility is derived from append-only `localization_qc_records` plus current synchronous eligibility checks.

There is also no stored `isolated` Boolean. Isolation means that no `localization_output_publications` row exists for the output, so it cannot drift out of sync with publication history.

`localization_output_publications` records the publication event (normal completion or authorized adoption) with the actor, role, and publication_kind. Owner or Member may adopt when the command policy permits, matching the Localization contract. An output can be published only once and only after all frozen QC checks have passed.

### Make provider-call reservation linkage mode-aware

For new calls, `provider_call_attempts.spend_reservation_id` is nullable only for canonical Mock and no-charge Sandbox calls; BYOK calls require exactly one SpendReservation. Mock and no-charge Sandbox still create a Job, ProviderCallAttempt, events/projection, capacity evidence, result evidence, and explicitly tagged simulated-cost evidence; they do not create a fake zero-value payable reservation or provider-cost ledger entry. Pre-existing Mock/Sandbox attempts with reservations retain those links under the read-only `legacy_reserved` migration value, which no new call may use.

### Let the dependency index represent package dependencies

`fact_library_dependencies` and `fact_market_policy_dependencies` (replacing the original `fact_dependency_index`) provide typed foreign keys to their respective targets. Library dependencies have a composite FK to `library_versions`; their nullable `truth_fact_id` distinguishes Fact-level dependencies from package-level ProductVersion, OfferVersion, and TruthResolutionPolicyVersion dependencies. Market-policy dependencies instead identify the exact `truth_package_market_id` and platform `MarketPolicyVersion`. Both tables reference `truth_package_id`, so freshness remains correct even when a market produced no raw Fact.

### Preserve failed and skipped source attempts

Add immutable `truth_source_attempts` rows so configured fallback history remains auditable even when an adapter produced no Fact. Each row stores `workspace_id`, `truth_package_id`, `truth_package_market_id`, `fact_type`, a concrete `scope_key`, `priority_ordinal`, DataSource and optional Mapping version IDs, adapter kind/version, resolution-rule version, exact requested context, `truth_data_mode`, Mock provenance when applicable, outcome (`accepted | failed | skipped`), conditional failure/evidence fields, and timestamps.

The table has both:

- `UNIQUE(workspace_id, truth_package_market_id, fact_type, scope_key, priority_ordinal)` to identify a policy slot; and
- `UNIQUE(workspace_id, id, truth_package_id, truth_package_market_id, fact_type, scope_key)` to support a tenant-safe same-package/same-scope composite foreign key.

Add `truth_package_id`, `scope_key`, and `accepted_source_attempt_id` to `truth_facts`. The Fact's composite foreign key includes `workspace_id`, `accepted_source_attempt_id`, `truth_package_id`, `truth_package_market_id`, `fact_type`, and `scope_key`, proving that it references the accepted attempt for the same package market and scope. An INSERT trigger additionally requires that attempt's outcome to be `accepted` and its requested-context/Mock provenance to match. `failure_code` is required only for failed attempts, `skip_code` only for skipped attempts, and `evidence_hash` only for accepted attempts.

## 2B. Fact-source seam — one interface, two adapters

Issue #24 defines one shared `FactSourceAdapter` interface and ships exactly two registered adapters:

- `ManualFactSourceAdapter`
- `ControlledMockFactSourceAdapter`

Do not add Shopify, Product Claim, or Shipping Policy stub classes. A future ticket adds a new adapter at this seam and registers it. `TruthResolutionPolicyVersion.source_priority` already references DataSource entries, read at their current effective DataSourceVersion (#70), so adding a future adapter does not require a TruthPackage schema migration.

The Creative Library may recognize a reserved `adapter_kind` before an executable adapter is installed. Publication validates the content schema and known kind. Resolution reports `adapter_not_enabled` when the current deployment has no registered implementation; an explicitly configured same-mode fallback may then run. No adapter silently changes modes.

```python
@dataclass(frozen=True)
class FactResolutionRequest:
    requested_fact_types: tuple[str, ...]
    offer: OfferContext
    market: MarketContext
    source_mapping: SourceMappingContext
    data_source: DataSourceConfig
    manual_fact_set: ManualFactSet | None
    truth_data_mode: Literal["real", "mock"]
    mock_scenario: str | None


FactValue = (
    MoneyFactValue
    | AvailabilityFactValue
    | DiscountFactValue
    | ShippingFactValue
    | DeliveryFactValue
    | ProductClaimFactValue
)


@dataclass(frozen=True)
class RawSourceFact:
    fact_type: str
    scope_key: str
    value: FactValue
    evidence_hash: str
    external_object_ref: str
    requested_context: Mapping[str, object]
    observed_at: datetime
    source_valid_until: datetime | None
    mock_scenario: str | None
    mock_version: str | None


@dataclass(frozen=True)
class FactSourceFailure:
    fact_type: str
    scope_key: str
    code: str
    message: str
    is_transient: bool


@dataclass(frozen=True)
class FactSourceResult:
    facts: tuple[RawSourceFact, ...]
    failures: tuple[FactSourceFailure, ...]


class FactSourceAdapter(Protocol):
    adapter_kind: str

    def resolve(self, request: FactResolutionRequest) -> FactSourceResult: ...
```

The request is persistence-free and secret-free. The localization module loads and validates ORM records, constructs value objects, selects the adapter, and persists the result. Requesting several fact types at once lets a future external adapter use one coherent source observation instead of repeating a storefront query for price, currency, and availability.

`available=false` is not a transport failure. It is a successful `AvailabilityFactValue(False)` with provenance and freshness. Adapter failures remain separate.

Manual facts are valid only when `valid_from <= resolution_time < valid_to`; publication and resolution both enforce the interval. Money remains Decimal plus ISO 4217 currency. The adapter never formats amounts or derives Claims.

Controlled Mock output is deterministic over adapter version, scenario, Offer context, market context, mapping, and requested fact set. Scenario/version enter evidence hashes, Truth fingerprints, and downstream lineage.

## 2C. Truth resolution module

`app/localization/truth.py` presents two main interfaces:

```python
resolve_or_reuse_truth_package(session, ctx, command) -> TruthResolutionResult
assess_truth_freshness(session, ctx, truth_package_id, *, at) -> FreshnessReport
```

`resolve_or_reuse_truth_package()` owns the complete algorithm:

1. Load the exact effective ProductVersion, OfferVersion, TruthResolutionPolicyVersion, requested MarketPolicyVersions, and MarketSourceMappingVersions.
2. Validate all records against the Batch Workspace and Offer/market meaning. Derive sources from policy and mappings; callers do not provide arbitrary source version IDs.
3. For each market, group unresolved fact types by the next configured DataSource and invoke its registered adapter.
4. Require the DataSource, Mapping, ManualFactSet when applicable, adapter result, and requested Batch `truth_data_mode` to agree. A real failure never falls back to Mock.
5. Follow `source_priority` only when `allow_fallback=true`. Every fallback remains visible in per-Fact provenance. If fallback is false, the first configured source is final.
6. After sources are exhausted, apply the frozen `on_failure` rule. Existing #20 behavior is authoritative: price forbids `degrade`, but permits `skip`. A skipped price can yield an executable Item only if all affected source-price Claims are removed and MarketPolicy does not require a local price; it can never preserve the source price.
7. Compute each Fact's effective expiry as the earliest applicable bound: source validity, `observed_at + policy.max_staleness`, and `observed_at + MarketPolicy.max_fact_staleness`. Version eligibility is checked separately and is not encoded only as a timestamp.
8. Freeze raw Facts first. Then deterministically create formatted money, evidence-backed stacked discounts, maximum claimable discount, and other derived Facts with explicit input Fact IDs and rule versions.
9. Derive per-market readiness and package `full | degraded | failed`. `available=false` produces `skipped_unavailable`; if no selected market is executable the package is `failed`.
10. Compute a canonical hash over every frozen input, exact mode/scenario, raw evidence, and derivation rule. Reuse a matching package only when `assess_truth_freshness()` still returns current.
11. Write the package, markets, raw Facts, derived Facts, derived inputs, and dependency edges atomically.

`assess_truth_freshness()` is the authoritative synchronous predicate. It checks `valid_until > at`, the effective lifecycle of every workspace LibraryVersion dependency, and the effective state of every platform MarketPolicyVersion dependency. It returns reasoned evidence, not only a Boolean. Binding freshness columns and events are rebuildable projections of this predicate.

## 2D. Generic provider-dispatch module

Generalize the existing dispatch and ProviderAttempt implementations into `app/providers/dispatch.py` and `app/providers/attempts.py`. Keep compatibility imports while generation migrates. The module is domain-agnostic: localization chooses the typed adapter and builds the typed request; `step_kind` is persisted metadata and is never a dispatch switch.

```python
RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")


class ExternalCallAdapter(Protocol[RequestT, ResultT]):
    provider: str
    capabilities: ProviderCapabilities

    async def execute(
        self,
        request: RequestT,
        *,
        credential: memoryview | None,
        idempotency_key: str,
    ) -> ProviderResult[ResultT]: ...


class ExternalCallCoordinator:
    async def execute(
        self,
        ctx: TenantContext,
        *,
        job_id: str,
        call_semantic_key_hash: str,
        step_kind: str,
        request: RequestT,
        adapter: ExternalCallAdapter[RequestT, ResultT],
        evidence_recorder: ExternalCallEvidenceRecorder[ResultT],
    ) -> CoordinatedCallResult[ResultT]: ...
```

`ExternalCallEvidenceRecorder` is an internal seam with three responsibilities: link the prepared ProviderCallAttempt to its domain subject in phase 1, materialize successful typed output into a durable redacted reference outside the database transaction, and persist that reference into domain evidence during phase 3. For analysis/translation the phase-1 subject is a pre-existing `localization_preparation_provider_calls` work row, not a result Inventory/CopyVersion that does not exist yet; phase 3 creates the immutable result and fills the work row's write-once result ID in the same transaction. Generation and Localization supply their own recorders at this seam; the shared coordinator never imports their ORM models.

The coordinator owns this three-phase implementation:

1. **Prepare in a transaction** — validate the Job, Operation, entitlement lease, provider selection, mode/chargeability relationship, chargeable reservation when required, capacity acquisition, credential state, and adapter/provider match; create the ProviderCallAttempt, initial event/projection, and domain join through the evidence recorder in the same transaction. Preparation calls link the existing work row; Item calls link the existing Attempt.
2. **Dispatch and materialize outside the transaction** — invoke the adapter with the coordinator-issued idempotency key and an ephemeral credential view, then use the evidence recorder to place successful output in durable storage and produce a redacted reference/hash. An orphaned object from a later database rollback is safe and can be garbage-collected.
3. **Complete in a transaction** — persist the domain result evidence through the recorder, append ProviderAttempt outcome/usage/certainty events, advance the CAS projection, write reported or estimated settlement evidence (with later Adjustment when required), release or retain reservations according to dispatch certainty, release capacity, and return typed output evidence.

For a successful localization edit, the immutable `localization_attempt_outputs` row and the ProviderAttempt success event commit in the same completion transaction. A crash after this commit but before the Localization fold replays from the durable output reference/hash and never recalls the provider. A crash after the network response but before this transaction commits leaves no committed result evidence and is classified `uncertain` until provider reconciliation.

The coordinator handles no Localization state transitions. The localization Worker consumes the coordinated result and updates its own Attempt/Item. Retry safety is derived from dispatch certainty plus adapter capabilities (`accepts_idempotency_key` and `supports_result_lookup_by_request_id`). `uncertain` is reconciled; it never creates a replacement call automatically.

Localization defines typed request/result value objects for source analysis, translation, image edit, OCR when external, and visual QC. Each registered provider adapter satisfies the generic interface for the step it supports. Controlled Mock is a registered adapter with deterministic scenario/version behavior; mock logic is not inlined in Worker handlers. No unimplemented real-provider stub is added.

## 2E. Staged preparation and command admission

Analysis and translation cannot be admitted together because translation depends on analysis plus user Claim decisions. The command sequence is:

| Command | Effect | Operation and Jobs |
|---|---|---|
| `localization.create_draft` | Persist selected sources and targets after discovery validation | No Operation |
| `localization.start_preparation` | Resolve/reuse Truth; reuse Inventories; start missing analysis | One `prepare_analysis` Operation with one external-call Job per missing Inventory |
| `localization.submit_claim_decisions` | Append confirmations and per-market decisions; plan/reuse/project copy; start missing translation | One `prepare_copy` Operation with one external-call Job per missing translated CopyVersion; no Operation when every version is reused or produced as a deterministic source-locale projection |
| `localization.approve_copy` | Append Owner/Member copy approvals | No Operation |
| `localization.confirm` | Recheck and freeze; create Items/Attempt 1; authorize edit and configured external QC calls | One Operation; one Job and, when chargeable, one SpendReservation per external call |
| `localization.retry_item` | Explicitly authorize Attempt 2 or 3 after a failed Attempt | One Operation; one Job and, when chargeable, one SpendReservation per external call |
| `localization.resume_item` | Recheck a paused Item and continue only already-authorized unspent work | No new paid call; if more paid work is required, route through the applicable explicit command |
| `localization.authorize_isolated_qc` | Explicitly authorize missing QC for a preserved isolated output | One Operation when external QC remains; one Job and, when chargeable, one SpendReservation per external call |
| `localization.adopt_isolated_output` | Recheck and publish an isolated output whose entire frozen QC plan has passed | No external call and no new Operation |
| `localization.cancel` | Cancel unstarted Jobs and release reservations | No new Operation |

`PaidOperationAdmission.admit_in_transaction()` remains the only admission path for commands with external calls. Final Batch freeze, Attempt-header creation, command/Operation linkage, Jobs, quotes, spend/capacity reservations, and Item CAS transitions commit or roll back together.

The final confirmation UI shows a frozen maximum-exposure envelope for all three permitted Attempts. The confirm Operation authorizes only Attempt 1. A `RetryItem` command re-quotes and rechecks the guardrail before authorizing Attempt 2 or 3. This preserves both bounded exposure and the invariant that a failed call never causes new spend without an explicit user intent.

The hard cap is three image-edit Attempts per Item, including the first. After Attempt 3 fails blocking QC, the Item becomes terminal `failed` with `reason_code=qc_exhausted`. A fourth Attempt is not valid for that Item; the user must create a new Batch with a new frozen intent.

## 2F. Localization module interfaces

### `app/localization/eligibility.py`

- `list_eligible_winners(session, ctx, *, requested_evidence_tier) -> list[EligibleWinner]` uses `ContextualVerdictProjectionRecord` only for discovery and returns exact source/experiment provenance for display.
- `validate_source_for_batch(session, ctx, asset_version_id, decision_id, *, requested_evidence_tier) -> ValidationReport` reads the authoritative `ExperimentDecisionRecord`, its confirmation and candidate, exact Winner context, current experiment eligibility projections, exact AssetVersion review/QC state, Product/Offer/Landing Page context, and source market snapshot.

The validation runs at selection, final confirmation, before every new external or paid step, and before publication/adoption.

### `app/localization/claims.py`

- `plan_inventories()` returns reused Inventories and missing analysis work; it does not pretend an asynchronous analysis result already exists.
- Analysis completion creates a write-once Inventory and Claims plus provider-call links.
- Low-confidence, conflicting, and high-risk Claims require append-only confirmation before per-market decisions are accepted.
- Claim decisions are market-scoped. `replace` requires a Fact from the same `truth_package_market`; `remove` requires a frozen removal strategy; `preserve` requires MarketPolicy to classify the Claim as market-neutral.

### `app/localization/copy.py`

- `plan_copy_versions()` returns reused versions, deterministic source-locale projections, and missing translation work. Locale equality skips only the external linguistic translation call; policy-mandated removal/replacement and later edit/QC remain in force.
- The copy series identity covers source AssetVersion, Inventory, target market, TruthPackage, Claim decision set, CopyPolicy snapshot, copy origin, translation recipe, and exact provider/model/adapter/mode/scenario when translation is used.
- Translation receives only translatable segments and opaque locked placeholders. Deterministic injection and validation happen after the external call.
- Reuse requires the complete semantic fingerprint, compatible evidence tier, and currently fresh Truth.
- `approve_copy_version()` appends an immutable approval by Owner or Member. It does not create a `QualityReviewRecord`; a localized AssetVersion is unreviewed because it has no human review decision yet.

### `app/localization/batch.py`

This module owns the command sequence in §2E, Batch/Item/Attempt CAS transitions, freeze invariants, idempotency, cancellation, resume, explicit retry, and outcome derivation.

`derive_batch_outcome()` runs only after every Item is terminal:

- `succeeded` when every Item succeeded or used `skipped_no_change_required`;
- `failed` when no market was executable, every executable Item failed/cancelled, or every market was unavailable;
- `partially_succeeded` otherwise.

`skipped_market_unavailable` never creates a binding. `skipped_no_change_required` creates a `source_reuse` binding and inherits the source AssetVersion's current review result only because it references that exact already-reviewed version; it does not create a new AssetVersion or copy a review row.

## 2G. One Job per external call and bounded QC

The frozen QC recipe declares which checks are local and which use an external provider. Every external call has its own Job and ProviderCallAttempt. A Job never hides several provider calls.

| Job type | External call | Result |
|---|---|---|
| `localization.analyze_source` | one analysis call | Inventory and Claims |
| `localization.translate_copy` | one translation call | translated segments; local injection/validation follows |
| `localization.edit_attempt` | one image-edit call | immutable isolated AttemptOutput |
| `localization.run_deterministic_qc` | none | locked Fact/number/currency/Claim-decision checks |
| `localization.ocr_check` | zero or one, as frozen by recipe | OCR check records |
| `localization.layout_check` | none in Beta | deterministic crop/overflow/region checks |
| `localization.visual_qc` | one visual comparison call | visual fidelity check records |
| `localization.finalize_item` | none | localized AssetVersion, lineage, and binding |
| `localization.fold_batch` | none | Batch phase/outcome projection |

If OCR later changes from local to external, it remains its own Job and reservation; `qc_item` is not reintroduced as a bundle.

For each Attempt:

1. The edit handler accepts only an authorized queued Attempt. A paused Item is not dispatchable.
2. Before the edit call, synchronously recheck source eligibility, Truth freshness, copy approval, Batch lifecycle, entitlement lease, and evidence-mode compatibility.
3. Run the coordinated call. Persist a successful response as an immutable isolated AttemptOutput before doing anything publishable.
4. Recheck eligibility after the response. If it changed in flight, keep the output isolated, stop unstarted paid QC, release its reservations, and move the Item to `paused` or `cancelled` with a precise recovery action.
5. Run deterministic QC first. If it fails blocking, do not spend on avoidable OCR/visual QC; cancel those unstarted Jobs and release their reservations.
6. If deterministic QC passes, run each frozen external QC call independently, with the same pre-call rechecks and three-phase dispatch.
7. Append one `localization_qc_record` per check. Attempt success is derived only when all required blocking checks pass.
8. On pass, enqueue/finalize locally. On failure before Attempt 3, move to `retry_wait`; no further paid call is scheduled. On failure at Attempt 3, move to terminal `failed/qc_exhausted`.

`attempt_count` increments only when a new immutable LocalizationAttempt header is created in the same transaction as its successful command admission. Job retry, Worker restart, provider reconciliation, and duplicate command delivery never increment it.

Before `finalize_item` publishes, it repeats every authoritative check. It then atomically creates the child AssetVersion, localized lineage, AttemptOutput adoption/publication relation, active MarketAssetBinding, and Item success transition. The child starts with no human QualityReview and no Winner verdict.

Concurrency is enforced by capacity admission/acquisition, not by counting active Job rows. Effective provider-call concurrency is the minimum of the localization cap (3), current Plan entitlement, provider limit, and available Workspace capacity. The Beta real-provider limit of 2 therefore wins where applicable.

## 2H. Cancellation and output adoption

Cancellation stops unstarted Jobs and releases their reservations. An indivisible dispatched call may complete; its usage/cost/certainty evidence and successful output are persisted, but the output stays isolated and never becomes an AssetVersion automatically.

`authorize_isolated_qc()` is an explicit Owner-or-Member command that admits only the missing checks from the frozen QC plan. It creates an Operation only when an external QC call remains. There is no QC waiver.

`adopt_attempt_output()` is a separate explicit Owner-or-Member command. It:

1. verifies the output has not already been adopted;
2. rechecks the exact source Winner, Batch/Item lineage, rights, Truth freshness, Copy approval, evidence tier, and current MarketPolicy;
3. requires all frozen blocking QC checks to have passed;
4. if required QC was never run, does not adopt directly—the user must first use `authorize_isolated_qc()` and wait for every required check;
5. records actor, role, recheck hash, and audit attribution; then uses the same atomic publication path as normal finalization.

No mutable `qc_passed` shortcut is consulted.

## 2I. Binding, freshness, and precise downstream invalidation

`app/localization/binding.py` owns this interface:

- `publish_binding()` atomically supersedes the active binding for the same `(workspace, source_asset_version, target_market, offer)` and inserts the new `localized` or `source_reuse` binding.
- `assess_binding_eligibility()` requires active lifecycle, a currently Approved exact AssetVersion with no blocking QC failure, synchronous current Truth, eligible upstream source/Winner context, and compatible rights/evidence tier.
- `apply_dependency_change()` folds a durable dependency-change event into affected binding events/projections.

Library publication/supersession/withdrawal uses the existing append-only `library_events` stream; MarketPolicy publication/supersession writes `market_policy_version_events` in the state-change transaction. An idempotent Worker resolves exact affected TruthPackages through `fact_library_dependencies` or `fact_market_policy_dependencies`, appends deduplicated `market_asset_binding_events`, and CAS-updates the rebuildable binding projection. Event identity includes the source event ID, dependency kind/version, new state, and binding ID.

Downstream experiment invalidation is precise. For each affected active binding, join its bound AssetVersion to the exact `ExperimentVersionCandidateRecord` rows and append `stale_truth_detected` with the affected `experiment_version_id` and `candidate_id`. A generic unscoped stale signal is insufficient. Frozen experiments keep their historical evidence, but new experiment confirmation and Launch are blocked.

The same durable pattern handles source rights withdrawal and upstream Winner/quality invalidation without rewriting TruthPackages, AssetVersions, or completed Attempts.

## 2J. Final module layout

```text
app/localization/
├── eligibility.py       # authoritative source/Winner validation
├── fact_sources.py      # FactSourceAdapter + Manual/ControlledMock adapters
├── truth.py             # resolution, derivation, dependency index, freshness
├── claims.py            # Inventory, confirmations, per-market decisions
├── copy.py              # translation planning, locked injection, approval
├── batch.py             # commands, freeze, state machines, retry/cancel/resume
├── worker.py            # thin durable Job handlers
├── qc.py                # deterministic checks and QC fold
└── binding.py           # publication, eligibility, stale propagation

app/providers/
├── adapter.py           # generic external-call adapter value types/interface
├── attempts.py          # append-only ProviderAttempt events + CAS projection
└── dispatch.py          # prepare/network/complete coordinator
```

These modules keep their interfaces small: callers ask for resolution, admission, transition, or eligibility results; ORM loading, policy evaluation, provider certainty, provenance writes, and CAS details remain inside the owning implementation.

---

# Section 3 (Final): Testing Strategy and Acceptance Evidence

This section is the authoritative testing plan for Issue #24. Tests exercise each module through its public interface. Direct table access is reserved for provenance evidence, database constraints, RLS, immutable triggers, projection rebuilds, and migration integrity.

## 3A. Test layers and organization

### Python module and integration tests

```text
tests/
├── contracts/
│   ├── test_fact_source_adapter_contract.py
│   └── test_external_call_adapter_contract.py
├── test_localization_eligibility.py
├── test_localization_evidence_tier.py
├── test_localization_truth.py
├── test_localization_truth_attempts.py
├── test_localization_business_rules.py
├── test_localization_claims.py
├── test_localization_copy.py
├── test_localization_batch.py
├── test_localization_worker.py
├── test_localization_qc.py
├── test_localization_cancel_adopt.py
├── test_localization_binding.py
├── test_localization_permissions.py
├── test_localization_routes.py
└── test_localization_integration.py
```

Module behavior tests call functions at the module interface or command handlers. They do not call private helpers or manufacture a successful domain state that the interface is required to protect.

### Browser tests

```text
web/e2e/localization/
├── golden-path.spec.ts
└── recovery.spec.ts
```

These tests cover UI → BFF → FastAPI → durable Worker → UI feedback with deterministic Controlled Mock adapters. They make no live Shopify or paid Provider calls.

### PostgreSQL-specific tests

```text
tests/
├── test_localization_rls.py
├── test_localization_triggers.py
├── test_localization_cas.py
└── test_localization_migration.py
```

SQLite remains the fast compatibility path. PostgreSQL is mandatory evidence for RLS/FORCE RLS, tenant roles and grants, immutable triggers, partial indexes, composite foreign keys, and concurrent CAS behavior.

## 3B. Fixture design

`LocalizationFixtures` provides upstream records and deterministic adapters without collapsing provenance dimensions:

```python
create_winner_source(
    *,
    source_generation_mode: Literal["byok", "sandbox", "mock"],
    experiment_evidence_mode: Literal["production", "sandbox", "mock"],
) -> WinnerSource

create_truth_inputs(
    *,
    truth_data_mode: Literal["real", "mock"],
    data_source_ids: Sequence[str],
) -> TruthInputs

create_batch_draft(
    *,
    provider_execution_mode: Literal["byok", "sandbox", "mock"],
) -> LocalizationBatch
```

The fixtures also provide:

- `seed_beta_market_policy_catalog()` for all nine target markets;
- published Product, Offer, DataSource, ManualFactSet, MarketSourceMapping, TruthResolutionPolicy, and BrandLocalizationPolicy builders;
- `fact_source_registry()` containing only Manual and Controlled Mock implementations;
- `localization_provider_registry()` containing deterministic per-step Controlled Mock implementations;
- an injectable clock and deterministic object store;
- Provider scenarios for success, blocking QC, unavailable market, delayed response, dispatch uncertainty, and result lookup.

Winner fixtures never accept or inspect `truth_data_mode`. Truth fixtures never set source or localization-provider provenance. Item creation occurs through Batch confirmation, not by direct fixture insertion when the behavior under test is admission or freeze.

## 3C. Evidence-tier derivation

Raw modes are normalized before comparison:

```python
def tier_of_source(mode: str) -> EvidenceTier:
    return {"byok": PRODUCTION, "sandbox": SANDBOX, "mock": MOCK}[mode]

def tier_of_experiment(mode: str) -> EvidenceTier:
    return {"production": PRODUCTION, "sandbox": SANDBOX, "mock": MOCK}[mode]

def tier_of_truth(mode: str) -> EvidenceTier:
    return {"real": PRODUCTION, "mock": MOCK}[mode]

def tier_of_provider(mode: str) -> EvidenceTier:
    return {"byok": PRODUCTION, "sandbox": SANDBOX, "mock": MOCK}[mode]
```

With `production > sandbox > mock`, Batch evidence is:

```python
evidence_tier = min_tier(
    *(tier_of_source(source.source_generation_mode) for source in batch.sources),
    *(tier_of_experiment(source.experiment_evidence_mode) for source in batch.sources),
    tier_of_truth(truth_package.truth_data_mode),
    tier_of_provider(batch.provider_execution_mode),
)
```

Required cases:

| Source/experiment inputs | Truth | Provider | Expected |
|---|---|---|---|
| all `byok/production` | real | byok | production |
| one source or experiment is sandbox | real | byok | sandbox |
| any source or experiment is mock | real or mock | any | mock |
| all production | real | sandbox | sandbox |
| all production | real | mock | mock |
| all production | mock | byok | mock |

Additional assertions:

- a TruthPackage containing both real and Mock DataSources is rejected, not downgraded;
- a caller cannot submit `evidence_tier`; the server derives it at freeze;
- a real TruthPackage consumed by a Mock provider is valid Controlled Mock evidence but cannot enter production reuse, experiment, Launch, or export;
- exact provenance modes and Mock scenario/version participate in semantic fingerprints even when two combinations yield the same derived tier;
- the fold includes every Batch source, not only the first.

## 3D. Fact-source adapter and TruthSourceAttempt contract

The shared `FactSourceAdapter` contract suite runs against Manual and Controlled Mock adapters. It proves:

- requests and results use typed persistence-free value objects;
- Decimal and ISO currency survive without float conversion;
- `AvailabilityFactValue(False)` is a successful Fact, not a failure;
- requested market, Offer, mapping and scope are honored exactly;
- Mock results are deterministic for the same version/scenario/input and differ across scenario/version;
- adapters do not format money, derive discounts, or query ORM state;
- partial results identify failures by fact type and concrete scope key.

Shopify is a recognized but unregistered adapter kind in this ticket:

- publishing a valid Shopify DataSourceVersion succeeds under the existing #20 schema;
- resolution produces `adapter_not_enabled` without importing or instantiating a stub;
- `allow_fallback=true` tries only the next source with the same `truth_data_mode`;
- without an eligible fallback, the frozen `on_failure` rule determines readiness.

Persistence tests for `truth_source_attempts` prove:

- uniqueness on `(workspace, package_market, fact_type, scope_key, priority_ordinal)`;
- accepted attempts require `evidence_hash` and no `failure_code`;
- failed attempts require `failure_code` but may have no evidence hash;
- skipped attempts require `skip_code` and neither failure nor accepted-evidence fields;
- the immutable trigger rejects update/delete;
- a Fact's composite `accepted_source_attempt_id` foreign key rejects cross-Workspace, cross-market, cross-type, and cross-scope references;
- first-source failure plus second-source success creates two rows and points the Fact to the accepted row;
- all-source failure preserves every attempt and the failed market readiness;
- the audit chain resolves Fact → accepted attempt → DataSourceVersion/Mapping → adapter/version/mode.

## 3E. Acceptance evidence for contract §16

### §16.1 — Source Winner eligibility

Reject each of the following through `validate_source_for_batch()`:

- no Decision, inconclusive Decision, or non-Winner Decision;
- missing confirmation;
- no human Approved review, rejected review, needs-changes review, or blocking QC failure;
- withdrawn Product/Offer, missing AssetVersion, or mismatched Product/Offer/Landing Page/placement/source market;
- source outside the initial `en-US/US/USD` contract;
- incompatible requested evidence tier;
- a localized source whose MarketAssetBinding is stale or otherwise ineligible.

`ContextualVerdictProjectionRecord` is tested only as a discovery index. A stale discovery projection may still display a Winner, but final validation rejects when the exact ExperimentVersion/Candidate has an ineligibility event such as `review_rejected` or `stale_truth_detected`. Decision, confirmation and candidate establish immutable identity and provenance; the current ExperimentEligibility event chain/projection establishes current eligibility.

### §16.2 — Batch source consistency

- different ProductVersion, OfferVersion, Workspace or source-market context is rejected at draft creation;
- duplicate source AssetVersion in one Batch is rejected;
- equal content hashes do not merge different Winner/Offer/Landing Page identities;
- an explicitly confirmed semantic-equivalence merge retains every original lineage reference;
- cross-Workspace attempts fail at the module interface and again under PostgreSQL RLS/composite foreign keys.

### §16.3 — Per-country market context

- all nine Beta MarketPolicyVersions use their own country, locale, currency and pricing context;
- DE, FR, ES and IT resolve independently despite sharing EUR;
- two markets may reuse a price source only when source evidence explicitly identifies the same price list, while retaining separate requested context and market rows;
- Manual and Controlled Mock evidence hashes differ when market context differs.

### §16.4 — Resolution, fallback and provenance

- Manual and Controlled Mock pass the same adapter contract;
- same-mode fallback records failed and accepted TruthSourceAttempts in priority order;
- cross-mode fallback is rejected;
- partial market/fact failures preserve per-Fact and per-attempt provenance;
- unregistered Shopify behavior follows §3D.

### §16.5 — No inferred market-sensitive facts

- missing target price never preserves source USD price or uses currency conversion;
- missing shipping, delivery, discount, component price or certification never copies source-market evidence;
- component price requires the component's own OfferVersion and source observation;
- discount stacking and maximum-savings Claims require explicit compatibility evidence;
- shipping promises respect country/region/postal-code granularity;
- price `on_failure=degrade` is rejected by #20 validation;
- price `skip` is allowed only when affected Claims are removed and MarketPolicy does not require a local price.

### §16.6 — Truth and Batch outcomes

- all markets ready → package `full`;
- permitted non-price omission → package `degraded` with visible warning;
- no executable market or core identity failure → package `failed` and no final confirmation;
- `available=false` → `skipped_market_unavailable` and no binding;
- success plus failure/skip → `partially_succeeded`;
- all executable Items failed and all remaining markets unavailable → Batch `failed`;
- `skipped_no_change_required` contributes to success only when its deterministic preconditions hold.

### §16.7 — Claims, locked copy and QC

- low-confidence, conflicting and high-risk Claims block copy generation until append-only confirmation;
- incomplete ClaimDecisionSets or a canonical hash that does not match the complete ordered decision rows cannot be reused or frozen into a Batch;
- `replace` references a Fact in the same TruthPackage market and scope;
- `remove` freezes a removal strategy and QC verifies the region is clear;
- `preserve` is accepted only for MarketPolicy-classified market-neutral content;
- translation receives opaque locked placeholders rather than mutable values;
- missing, duplicated, reordered-invalid or additional placeholders fail validation;
- wrong number, currency, unit, certification or delivery range is blocking;
- new unsupported commercial Claims are blocking;
- protected terms and prohibited brand phrases are enforced;
- non-blocking warnings remain visible for human review;
- structured QC feedback from Attempt N is included in the frozen input evidence for Attempt N+1.

The claim/copy policy suite is parameterized over all nine Beta markets. Locale equality controls only whether linguistic translation is needed; it never bypasses Claim decisions, Fact replacement, policy edits, or QC:

| Target | Source locale condition | Frozen policy/Fact condition | Expected path |
|---|---|---|---|
| `en-GB` | source locale = `en-GB` | market-neutral copy, no target-specific change | no translation call; deterministic `skipped_no_change_required`/`source_reuse` after all eligibility checks |
| `en-AU` | source locale = `en-AU` | restricted Claim must be removed | no translation call; removal edit and full frozen QC plan required |
| `en-CA` | source locale = `en-CA` | price/currency Claim has a target Fact replacement | no translation call; locked-value edit and deterministic number/currency QC required |
| `de-DE` | source locale differs | translatable copy plus locked EUR Fact | translation, deterministic injection, edit, and QC |
| `fr-FR` | source locale differs | protected brand term | translation preserves the term; edit and QC |
| `es-ES` | source locale differs | prohibited phrase in proposed copy | translation result rejected before approval/publication |
| `it-IT` | source locale differs | market-neutral Claim preserved with policy evidence | translation plus preserve-policy validation, edit, and QC |
| `ja-JP` | source locale differs | market requires local copy | translation cannot be skipped even when the source has few translatable segments |
| `ko-KR` | source locale differs | unavailable target Offer | no translation/edit call; `skipped_market_unavailable` |

Each row asserts ProviderCall counts, ClaimDecision persistence, CopyVersion state (or justified absence), Item outcome, and binding/publication behavior—not only returned text.

### §16.8 — Bounded retries, idempotency and cost

- final confirmation creates Attempt 1 only;
- blocking failure moves the Item to `retry_wait` without automatic spend;
- explicit RetryItem creates Attempt 2 or 3 through a new command and Operation;
- Attempt 3 blocking failure produces terminal `failed/qc_exhausted`;
- Attempt 4 is rejected;
- `attempt_count` increments only when admission and immutable Attempt creation commit;
- duplicate commands, Jobs and folds do not duplicate provider calls, costs, AttemptOutputs or AssetVersions;
- `uncertain` does not exit without reconciliation evidence.

Cost expectations depend on dispatch certainty, not QC:

| Provider outcome | Cost action |
|---|---|
| confirmed not dispatched | release chargeable reservation |
| completed chargeable call | reported or estimated settlement; later Adjustment allowed |
| may have dispatched | reservation remains uncertain |
| edit settled, deterministic QC later fails | edit remains settled; only unstarted QC reservations release |
| new canonical Mock or no-charge Sandbox call | no payable SpendReservation/provider-cost entry; retain Job, attempt, events, capacity and simulated-cost evidence |

Every external call has exactly one Job and ProviderCallAttempt. Every chargeable call has exactly one SpendReservation. A local deterministic Job has none.

### §16.9 — Semantic reuse

- identical complete Item fingerprint reuses the original successful output with no call or cost;
- an in-progress fingerprint returns the original Operation;
- a failed fingerprint returns the original failure until explicit RetryItem;
- changing TruthPackage, CopyVersion, MarketPolicyVersion, Claim decisions, recipe, any raw provenance mode, or Mock scenario/version creates new work;
- Inventory and Copy reuse use their narrower independent fingerprints across Batches;
- stale Truth or incompatible evidence tier prevents reuse;
- content hash and filename alone never identify semantic reuse.

### §16.10 — Cancellation and runtime invalidation

- cancellation stops unstarted Jobs and releases their reservations;
- an in-flight atomic call may complete and records cost/certainty plus an isolated AttemptOutput;
- source/rights/Truth/Entitlement invalidation before a new external step pauses the Item without calling;
- invalidation after response preserves output but prevents QC/publication as applicable;
- resume requires an explicit command and continues only already-authorized unspent work;
- any new paid call routes through a new authorized command;
- restoration does not auto-resume.

### §16.11 — AssetVersion lineage and review

- successful publication creates `origin_kind=localized` and an immutable parent source AssetVersion;
- lineage identifies Batch, Item, Winner Decision, Product, Offer, source/target market, TruthPackage, Inventory, CopyVersion, AttemptOutput, QC and all provenance modes;
- no QualityReview row or Winner verdict is copied or created;
- binding starts `pending_review` and is not experiment-ready;
- exact-version human approval makes it eligible only if all other synchronous binding checks still pass;
- later review rejection or upstream invalidation removes readiness without deleting history;
- `source_reuse` references the exact existing reviewed source version and does not clone its review row.

### §16.12 — Stale propagation

Superseding or withdrawing each real dependency—DataSourceVersion, ManualFactSetVersion, MarketSourceMappingVersion, OfferVersion, ProductVersion, TruthResolutionPolicyVersion or MarketPolicyVersion—invalidates only affected bindings. Credential rotation is a negative control and must not stale Truth.

Tests also prove:

- historical TruthPackages, Facts, Attempts and AssetVersions remain unchanged;
- event delivery is idempotent;
- projection rebuild equals incremental fold;
- downstream `stale_truth_detected` targets the exact ExperimentVersion and Candidate;
- frozen experiments retain historical evidence while new experiments and Launches are blocked;
- source rights and upstream review/Winner invalidation use the same durable propagation pattern.

### §16.13 — Permissions, tenancy and redaction

- Member cannot publish/withdraw DataSourceVersion, OfferVersion, ManualFactSetVersion or BrandLocalizationPolicyVersion;
- Owner publication requires the existing MFA step-up, confirmation and audit requirements;
- Member and Owner can perform the Localization commands allowed by the accepted contract, including copy approval, start/cancel/retry, isolated-QC authorization, adoption and human review;
- Worker scope comes from the trusted Job and TenantContext, never payload-supplied Workspace identity;
- cross-Workspace reads/writes fail through the interface, RLS and composite foreign keys;
- exports and audit views omit credential plaintext/ciphertext, KMS metadata, signed URLs, raw Provider payloads and access-bearing object identifiers;
- exact modes and Mock scenario/version prevent cross-mode and cross-scenario reuse or aggregation.

### §16.14 — Browser golden path

The Playwright golden path uses at least three markets:

1. start from an existing Approved, confirmed Winner;
2. select exact source, Offer and markets;
3. preview Truth with provenance and readiness;
4. complete Inventory analysis and per-market Claim decisions;
5. generate, validate and approve copy;
6. confirm the maximum-exposure envelope and freeze the Batch;
7. process one successful Item, one deterministic no-change/unavailable Item, and one blocking-QC Item;
8. show `retry_wait`, explicitly authorize Attempt 2, and succeed;
9. show the new localized child as pending human review;
10. approve it and verify the exact target-market experiment entry becomes available.

The recovery browser test cancels during a delayed Controlled Mock edit, verifies the returned output remains isolated, verifies direct adoption is blocked, explicitly authorizes missing QC, waits for all frozen checks to pass, adopts once, and confirms the resulting AssetVersion still starts unreviewed.

## 3F. Staged preparation and freeze evidence

Required command-sequence tests:

- analysis incomplete → translation admission rejected;
- unresolved high-risk Claim → translation admission rejected;
- all Inventory/Copy work reused or deterministically projected → no fabricated Operation, Job or Reservation;
- entitlement, capacity or guardrail failure → no frozen Batch, command/Operation link, Attempt, Job or Reservation commits;
- a frozen field cannot be rewritten;
- same semantic command replays the original Operation and result;
- Analysis and Translation use separate Operations;
- final confirmation atomically freezes Batch/context, creates Items and Attempt 1, links command/Operation, and admits each external-call Job;
- deterministic QC failure prevents avoidable external OCR/Visual QC and releases only their unstarted reservations;
- all Item transitions use CAS and a losing writer reloads rather than overwriting.

The maximum-exposure envelope covers at most three permitted Attempts, while each confirmation/retry command reserves only the calls it currently authorizes. Guardrails are re-evaluated for RetryItem.

## 3G. Provider dispatch, crash and reconciliation evidence

The generic adapter contract runs against at least two adapters at the seam: the Controlled Mock implementation and an in-memory deterministic adapter used to vary capability behavior. It verifies typed request/result handling, credential lifetime, provider/adapter matching, idempotency-key propagation, result materialization and redaction.

The coordinator never chooses an adapter from `step_kind`. Tests pass different typed adapters for the same metadata and prove caller selection controls behavior; a reservation/provider mismatch is rejected before dispatch.

Five durability windows are injected explicitly:

| Window | Last committed evidence | Recovery |
|---|---|---|
| Job claimed before prepare | Job claim only | reclaim and prepare idempotently |
| ProviderAttempt prepared before network | `not_dispatched/prepared` Attempt | redispatch the same authorized call |
| response received before completion commit | dispatch started, no result evidence | uncertain; reconcile with provider evidence |
| completion committed before domain fold | durable result reference/hash, domain AttemptOutput and ProviderAttempt success event | replay fold without provider call or duplicate cost |
| provider may have received request, no response | uncertain Attempt, no result evidence | capability-aware reconciliation; never blind retry |

Localization supplies an `ExternalCallEvidenceRecorder` implementation. The coordinator invokes it without importing Localization models. For successful edit completion, the AttemptOutput and ProviderAttempt success event commit in one transaction.

Reconciliation follows evidence, not a simplistic Boolean matrix:

1. if a queryable provider request ID exists, lookup first;
2. if lookup returns a completed result, materialize and fold it;
3. retry with the same key only when the provider contract/capability guarantees idempotency for that key;
4. if lookup is unavailable or inconclusive and idempotency is not guaranteed, retain `uncertain` for manual reconciliation;
5. reconciliation replay never duplicates settlement or domain output.

Capacity tests prove effective concurrency is `min(3, Plan entitlement, provider limit, Workspace available capacity)`. Beta real-provider capacity 2 wins over the localization target 3, and provider limit 1 serializes calls even when Workspace capacity is higher.

## 3H. Isolated-output QC and adoption

There is no QC waiver and no `AttemptOutput.qc_passed` field.

Required sequence:

1. cancelled/ineligible in-flight edit returns an immutable isolated output;
2. direct adoption fails while the frozen QC plan is incomplete;
3. Owner or Member explicitly invokes `authorize_isolated_qc`;
4. admission creates one Job per missing external QC call and a SpendReservation only when chargeable;
5. deterministic and external checks append LocalizationQCRecords;
6. any blocking failure leaves the output isolated;
7. after every frozen blocking check passes, adoption rechecks source Winner, rights, Truth freshness, Copy approval, exact MarketPolicyVersion state and evidence-tier compatibility;
8. `adopt_isolated_output` uses the same atomic publication path as normal finalization;
9. the new AssetVersion has no QualityReview and the output cannot be adopted twice.

Adoption eligibility is derived from the complete LocalizationQCRecord chain plus synchronous current eligibility. Human QualityReview applies only after AssetVersion publication.

## 3I. Migration and PostgreSQL evidence

The migration test starts from the new migration file's declared `down_revision`, inserts representative existing rows, upgrades to head, and verifies every old row and foreign-key chain remains intact.

Existing-table evolution cases include:

| Table | Evolution evidence |
|---|---|
| `provider_call_attempts` | old `generation/visual_qc` rows survive, including reserved Mock/Sandbox history under read-only `legacy_reserved`; wider step kinds work; all three new-call mode/chargeability/reservation combinations pass and every mismatch/new legacy insertion fails; Truth-required step kinds reject NULL/orphan/cross-Workspace packages |
| `asset_versions` | existing upload/generated rows survive; preflight reports any orphaned legacy Truth IDs; localized origin requires same-Workspace Truth and a parent version |
| `quality_reviews` | existing exact-version approved/rejected review history is unchanged |
| provider attempt event/projection tables | generation rows and compatibility imports continue to work after generic-module extraction |

New tables are tested through a clean upgrade and PostgreSQL metadata assertions for:

- tenant-safe parent-carrying composite foreign keys, including deliberate cross-Batch, cross-source, cross-market, cross-scope, and cross-Workspace rejection cases;
- partial active-binding and effective-policy indexes plus unconditional terminal-state semantic-claim uniqueness;
- immutable/write-once triggers;
- RLS plus FORCE RLS and expected grants;
- enum/check constraints, including TruthSourceAttempt provenance, publication-kind fields, and provider chargeability;
- preparation work created before dispatch, linked to its ProviderAttempt in phase 1, and filled with exactly one result ID on successful phase-3 completion;
- MarketPolicy publication/supersession commits its `market_policy_version_events` outbox row atomically and replays without duplicate binding events;
- CAS conflicts across two real database sessions;
- projection rebuild after deleting only the rebuildable projection, never immutable evidence.

SQLite migration tests verify compatibility and full metadata creation, but are not accepted as evidence for PostgreSQL-only behavior.

## 3J. Completion gate

Issue #24 is complete only when:

- every Contract §16 case above has an automated test mapped to it;
- the Manual and Controlled Mock FactSource adapters pass the shared contract suite;
- no paid/external call path lacks Job, attempt, certainty and mode-appropriate cost evidence;
- all nine market fixtures pass currency/context/freshness checks;
- Python, PostgreSQL and Playwright suites pass in CI;
- generated manifests and user-facing degraded/paused/uncertain states contain stable reason codes and safe next actions;
- no test relies on live Shopify or an unimplemented Adapter stub.

---

# Implementation Order

## Phase 1: Foundation (no external calls)

1. **Platform tables**: `market_policies`, `market_policy_versions`, `market_policy_version_events` — seed the nine-market catalog and its transactional lifecycle outbox
2. **Truth tables**: `truth_packages`, `truth_package_markets`, `truth_source_attempts`, `truth_facts`, `truth_derived_facts`, `truth_derived_fact_inputs`, `fact_library_dependencies`, `fact_market_policy_dependencies`
3. **Existing table migration**: preflight and evolve `asset_versions` (origin_kind, Truth FK, localized-parent CHECK), then evolve `provider_call_attempts` (wider step kind, mode-aware nullable reservation, relational chargeability CHECK, Truth FK/required-step CHECK). Follow §1.11's explicit DDL order inside the single revision.
4. **Library contract activation**: register and validate `ENTRY_BRAND_LOCALIZATION_POLICY` across Library contracts, hashes/dependencies, manifests, lifecycle/permissions, export/redaction, and tests; activate localization `WITHDRAWAL_DOWNSTREAM_EVENT_TYPES`

## Phase 2: Fact-source and truth resolution

5. **`app/localization/fact_sources.py`**: `FactSourceAdapter` protocol, Manual and ControlledMock adapters
6. **`app/localization/truth.py`**: resolution algorithm, freshness predicate, canonical hash, and both typed dependency writers/readers

## Phase 3: Generic provider dispatch

7. **`app/providers/adapter.py`**: `ExternalCallAdapter` protocol, `ProviderCapabilities`, `ExternalCallEvidenceRecorder`
8. **`app/providers/dispatch.py`**: three-phase `ExternalCallCoordinator`
9. **`app/providers/attempts.py`**: generic append-only events and CAS projection
10. Generation compatibility: re-export under existing import paths

## Phase 4: Localization domain

11. **Batch core tables**: `localization_batches`, `localization_batch_sources`, `localization_batch_commands`, `localization_batch_market_targets`; defer the command → Item FK
12. **Inventory tables**: `source_claim_inventories`, `source_claim_confirmations`
13. **Claim decision tables and module**: `localization_claim_decision_sets`, `localization_claim_decisions`, `app/localization/claims.py`
14. **Copy tables and module**: `localized_copy_versions`, `localized_copy_approvals`, `app/localization/copy.py`
15. **Item/attempt tables**: `localization_items`, then the deferred command FK, followed by `localization_attempts`, `localization_attempt_outputs`, `localization_output_publications`, `localization_qc_records`
16. **Provider and idempotency tables**: `localization_item_provider_calls`, `localization_preparation_provider_calls`, `localization_semantic_claims`
17. **Eligibility module**: `app/localization/eligibility.py`
18. **Batch module**: `app/localization/batch.py` — commands, freeze, state machines
19. **QC module**: `app/localization/qc.py` — deterministic checks
20. **Worker module**: `app/localization/worker.py` — Job handlers

## Phase 5: Binding and lineage

21. **Binding tables**: `market_asset_bindings`, `market_asset_binding_events`, `localized_asset_lineages`
22. **Binding module**: `app/localization/binding.py` — publication, eligibility, stale propagation

## Phase 6: API and UI

23. **FastAPI routes**: `app/localization/routes.py`
24. **BFF bridge routes**: localization command and query endpoints
25. **Web UI**: localization workspace pages

## Phase 7: Evidence

26. **Python test suite**: all `test_localization_*.py` files — execute the explicit nine-market/source-locale matrix in §16.7, including equal-locale no-change, removal, and locked Fact replacement paths
27. **PostgreSQL tests**: RLS, triggers, CAS, ordered migration, exact composite FKs, partial/effective indexes, terminal semantic uniqueness, chargeability, and outbox atomicity
28. **Playwright tests**: `golden-path.spec.ts`, `recovery.spec.ts`
