# Issue #20: Templates, Prompt Policy, DataSources & ResolutionPreview

Status: Draft  
Date: 2026-09-01  
Scope: Creative Library extensions — DimensionTemplate, PromptPolicy, DataSource, ResolutionPreview  
Blocked by: #18 (closed), #19 (closed)  
Contracts: `creative-library-ownership-and-reuse-contract.md` §9–§13, `v9-functional-alignment-acceptance.md`

---

## 1. Overview

Complete the Creative Library with four subsystems:

1. **Dimension Templates** — DimensionTemplate/Version, DimensionConceptKey, Apply/reapply-as-new-Draft, combination constraints with bounded satisfiability.
2. **Prompt Policy** — PlatformPromptBaselineVersion, WorkspacePromptPolicyVersion, deterministic single-pass composition with registered tokens and locked phrases.
3. **Data Sources** — DataSourceDefinition/Version, CredentialBinding, ManualFactSetVersion, TruthResolutionPolicyVersion; manual and controlled Mock adapters only.
4. **Resolution Preview** — freeze all library versions, constraint results, prompt assembly, provider decisions, cost quote; deterministic canonical hashing with selector-aware invalidation.

All subsystems reuse the existing `LibraryEntry → LibraryDraft → LibraryVersion` three-layer model. Generic CRUD commands (create/edit/validate/publish/withdraw/archive) flow through existing `service.py` infrastructure. Only domain-specific commands (`apply_template`, `reapply_template`, `compose_prompt`, `resolve_preview`, `bind_credential`) require new command interfaces.

## 2. Code Organization

```
app/library/
├── __init__.py
├── api.py              # HTTP endpoints (extend with new routes)
├── contracts.py        # Add validators + entry types for new types
├── references.py       # Existing reference pipeline (unchanged)
├── service.py          # Existing commands + shared helpers (unchanged interface)
├── templates.py        # apply_template, reapply_template, constraint solver
├── prompt_policy.py    # Platform baseline, composition, locked-phrase normalization
├── datasources.py      # DataSource, CredentialBinding, ManualFactSet, TruthResolutionPolicy
└── preview.py          # ResolutionPreview freeze, hash, invalidation
```

New ORM models in `app/adapters/persistence/models.py`. New migration for tables. Legacy `DimensionSchemaRecord`/`DimensionRecord`/`DimensionValueRecord` coexist untouched; new system uses library Entry/Draft/Version tables.

## 3. Data Model

### 3.1 New Entry Types

Added to `contracts.py`:

| Entry Type | `entry_type` value | Owner-only publish? | Notes |
|---|---|---|---|
| Dimension Template | `dimension_template` | No | Template content with dimensions, values, constraints, concept keys |
| Dimension Schema | `dimension_schema` | No | Product-local applied schema; immutable `product_entry_id` |
| Prompt Policy | `prompt_policy` | Yes | WorkspacePromptPolicyVersion |
| Data Source | `data_source` | Yes | DataSourceVersion with adapter config |
| Manual Fact Set | `manual_fact_set` | Yes | Typed facts with decimal strings |
| Truth Resolution Policy | `truth_resolution_policy` | Yes | Per-fact-type source priority and failure behavior |

`OWNER_ONLY_PUBLISH` updated: `{offer, prompt_policy, data_source, manual_fact_set, truth_resolution_policy}`.

`ORIGIN_COMMANDS` extended with `COMMAND_APPLY = "apply"`.

### 3.2 New Tables

#### `dimension_concept_keys`

Workspace-scoped or platform governance identity for cross-product dimension concepts.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) PK | `new_id("dck")` |
| `workspace_id` | String(64) nullable | FK → workspaces; NULL for platform scope |
| `scope` | String(20) | CHECK `IN ('workspace', 'platform')` |
| `name` | String(300) | Display name; NOT identity |
| `description` | Text | |
| `created_by` | String(100) | |
| `created_at` | DateTime(tz) | |

Scope check constraint: `(scope = 'workspace' AND workspace_id IS NOT NULL) OR (scope = 'platform' AND workspace_id IS NULL)`.

Identity is `id`, never `name`. Cross-product aggregation requires matching `id` plus comparable experiment context.

#### `prompt_baselines`

Platform-level `PlatformPromptBaselineVersion`. Not workspace-scoped — separate authorization seam outside Workspace RLS.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) PK | `new_id("pbl")` |
| `version_no` | Integer | CHECK ≥ 1, UNIQUE |
| `content_json` | JSON | Safety/fidelity/provider-limit clauses with stable clause_ids |
| `content_hash` | String(64) | |
| `state` | String(20) | CHECK `IN ('effective', 'superseded', 'withdrawn')` |
| `published_by` | String(100) | |
| `published_at` | DateTime(tz) | |
| `superseded_at` | DateTime(tz) nullable | |
| `withdrawn_at` | DateTime(tz) nullable | |
| `withdraw_reason` | Text | default `''` |

At most one `effective` version enforced by partial unique index. State transitions: `effective → superseded`, `effective → withdrawn`. Emergency withdraw persists outbox/domain event + impact records for downstream (#21) consumption.

Platform token definitions are embedded in baseline content; they are separate from Workspace-registered tokens. A new baseline never depends on token declarations controlled by a Workspace policy.

#### `datasource_credential_bindings`

Links a DataSource library entry to a `CredentialVersion` from #18. Named `datasource_credential_bindings` to avoid collision with the existing `credential_bindings` table (#18), which maps credentials to provider/scope.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) PK | `new_id("dcb")` |
| `workspace_id` | String(64) | FK → workspaces |
| `data_source_entry_id` | String(64) | Composite FK (workspace_id, data_source_entry_id) → library_entries |
| `credential_version_id` | String(64) | Composite FK (workspace_id, credential_version_id) → credential_versions |
| `revision` | Integer | CHECK ≥ 1 |
| `state` | String(20) | CHECK `IN ('active', 'superseded')` |
| `created_at` | DateTime(tz) | |
| `updated_at` | DateTime(tz) | |

UNIQUE `(data_source_entry_id, revision)`. Partial unique index: at most one `active` binding per `data_source_entry_id`.

Binding states are `active`/`superseded` only. Credential revocation is `CredentialVersion` eligibility state, not overloaded onto the binding.

#### `resolution_previews`

Frozen preview snapshots with a single authoritative `snapshot_json` aggregate.

| Column | Type | Constraints |
|---|---|---|
| `id` | String(64) PK | `new_id("rpv")` |
| `workspace_id` | String(64) | FK → workspaces |
| `generation_draft_id` | String(64) | |
| `draft_revision` | Integer | |
| `snapshot_json` | JSON | Canonical, schema-versioned immutable aggregate |
| `snapshot_schema_version` | String(20) | e.g. `"20.1"` |
| `preview_hash` | String(64) | Indexed; `canonical_hash(snapshot_json)` |
| `recipe_input_hash` | String(64) | Indexed; excludes time-bound fields |
| `execution_mode` | String(10) | Indexed |
| `valid_until` | DateTime(tz) | min(quote, rights, facts, entitlement expiry) |
| `created_by` | String(100) | |
| `created_at` | DateTime(tz) | |
| `invalidated_at` | DateTime(tz) nullable | |
| `invalidation_reason` | String(300) | default `''` |
| `submitted_run_id` | String(64) nullable | One-time submission linkage |
| `submitted_at` | DateTime(tz) nullable | |

`snapshot_json` is the single authoritative immutable aggregate. `preview_hash`, `recipe_input_hash`, `execution_mode`, and `valid_until` are indexed projections derived from it. This ensures no newly added preview field is accidentally omitted from hashing.

`recipe_input_hash` excludes time-bound fields (quote expiry, eligibility evidence, binding revision, confirmation decisions). `preview_hash` includes everything.

Credential secrets are never frozen into recipe content. The snapshot records `credential_evidence: {binding_revision, eligible}` so rotation invalidates unsubmitted previews; the credential actually used is recorded in each external-call record (#21).

## 4. Commands & Validation

### 4.1 Dimension Templates

#### Content validation (in `contracts.py`)

DimensionTemplateVersion content shape:

```
{
  "dimensions": [
    {
      "id": "stable-dim-id",
      "name": "...",
      "values": [
        {
          "id": "stable-val-id",
          "name": "...",
          "positive_fragments": ["..."],
          "negative_fragments": ["..."]
        }
      ],
      "creative_brief": { ... }  // validated against pinned JSON Schema
    }
  ],
  "constraints": [
    {"type": "requires", "from_dimension_id": "...", "from_value_id": "...", "to_dimension_id": "...", "to_value_id": "..."},
    {"type": "excludes", "dimension_id_a": "...", "value_id_a": "...", "dimension_id_b": "...", "value_id_b": "..."},
    {"type": "max_occurrence", "dimension_id": "...", "max": 1}
  ],
  "concept_key_mappings": [
    {"dimension_id": "...", "value_id": "...", "concept_key_id": "..."}
  ],
  "creative_brief_schema_version": "sha256-of-schema",
  "combination_cardinality": "one_per_dimension"  // explicit: each combo selects exactly one value per dimension
}
```

Validation rules:
- All `id` fields within a template must be unique.
- `creative_brief` validated against the JSON Schema identified by `creative_brief_schema_version`. Schema version/hash pinned in published content so future Schema changes cannot alter historical validation.
- `concept_key_mappings` reference `DimensionConceptKey` IDs that must exist and have matching scope (same workspace or platform).
- `combination_cardinality` explicitly defines how combinations are formed. When `one_per_dimension`, `max_occurrence` on a dimension is always 1 and adds no behavior.
- Constraints reference stable dimension/value IDs within the same template.

#### Constraint analysis

Two distinct checks:

1. **Cycle detection** — `requires` chains forming A→B→C→A are a forbidden authoring pattern. Reported as a blocking Finding even though they may be logically satisfiable.

2. **Satisfiability** — a bounded solver (not brute-force expansion) verifies at least one valid combination exists given all `requires`/`excludes`/`max_occurrence` constraints. Zero valid combinations → blocking Finding.

Combination expansion is bounded. `evaluate_constraints(schema, planned_run_items)` returns planned RunItems plus structured `SkipReason` records, with a hard combination limit. Exceeding the limit → blocking domain error with no quote, reservation, or preview persisted.

A `SkipReason` may cite multiple violated constraints. The output is either all violations or a deterministic primary reason plus an `all_violations` list.

#### Apply template (`templates.py`)

`apply_template(context, template_version_id, product_entry_id, *, idempotency_key, request_fingerprint)`:

1. Validate `product_entry_id` identifies an active Product entry in the same Workspace.
2. Deep-copy every dimension/value from the template, minting new product-local UUIDs.
3. Remap constraint references to new local IDs.
4. Atomically create a new `dimension_schema` LibraryEntry and its Draft in one transaction, with:
   - `origin_command = COMMAND_APPLY`
   - `source_version_id = template_version_id`
   - `product_entry_id` in content (immutable across versions of this entry)
   - Per-dimension: `source_dimension_id`, `source_value_id` provenance
   - `derived_from_template_version_id` in content
5. Failed apply leaves no orphan schema Entry.
6. Idempotency: same key replays the original draft; same key + different fingerprint → rejected.

#### Reapply template (upgrade)

`reapply_template(context, new_template_version_id, existing_schema_version_id, *, idempotency_key, request_fingerprint)`:

Three-way comparison:
- **Base**: the original template version the schema was derived from (via `derived_from_template_version_id`).
- **Theirs**: the new template version being reapplied.
- **Ours**: the current published DimensionSchemaVersion (with local edits).

Returns a `TemplateDiffReport`:
- Template-only changes (additions/modifications/deletions in new template vs base).
- Local-only changes (edits made to the schema after apply).
- Conflicts (both template and local changed the same dimension/value).

User selects items → creates a new DimensionSchema Draft. Local IDs for retained dimensions/values are preserved. No auto-push.

### 4.2 Prompt Policy

#### PlatformPromptBaselineVersion (`prompt_policy.py`)

Platform-scoped. Managed by platform operators via separate authorization seam.

Content:
```
{
  "clauses": [
    {
      "clause_id": "stable-id",
      "category": "safety|fidelity|provider_limit",
      "text": "...",
      "tokens": [{"name": "...", "required": true, "default": "..."}],
      "locked": true
    }
  ],
  "platform_tokens": [
    {"name": "...", "description": "...", "required": true}
  ]
}
```

Platform tokens are defined here, separate from Workspace-registered tokens. A new baseline never depends on Workspace token declarations.

Commands: `publish_baseline`, `withdraw_baseline`. Emergency withdraw persists an outbox/domain event and impact records (durable for #21 consumption), not just a returned list.

#### WorkspacePromptPolicyVersion

Owner-only publish. Content:
```
{
  "registered_tokens": [
    {"name": "...", "description": "...", "required": true, "default": "...", "validation_regex": "..."}
  ],
  "locked_phrases": [
    {"phrase": "...", "clause_id": "...", "protected": true}
  ],
  "brand_tone": { ... },
  "allowed_template_entry_ids": ["..."],  // entry IDs; resolution uses latest effective version
  "optional_clauses": [
    {"clause_id": "...", "text": "...", "enabled": true}
  ]
}
```

Field is `allowed_template_entry_ids` (not `allowed_template_ids`) — references stable entry IDs. Resolution always uses the latest effective version of each allowed template.

#### Locked phrase normalization

Protected-phrase handling is a save-time normalization, not a validation-time mutation:

1. When saving a draft that removes a protected phrase, the save operation restores the full protected clause.
2. The draft `revision` is incremented.
3. A `warning` Finding is added to the draft's validation state.
4. Publication is blocked until the warning is acknowledged via the existing `acknowledged_warnings` mechanism.

Validation remains a pure function that does not mutate content.

#### Deterministic composition

`compose_prompt(baseline_version, policy_version, run_inputs) → ComposedPrompt`

Pure function. Steps:
1. Platform baseline clauses (order by `clause_id`).
2. Workspace policy clauses (respecting locked phrases).
3. Run inputs: product description, must-match, person fragments, dimension values, intent, aspect ratio, negatives, QC feedback.
4. Single-pass token substitution: scan for `{{token_name}}`, replace with registered value. Rules:
   - Unknown token → error (blocking).
   - Missing required token → error (blocking).
   - Recursive token definition in authoring (token A defined as containing `{{B}}`) → blocked at publish.
   - Runtime substitution value that happens to contain `{{...}}` → remains literal, not rescanned.
5. Return `ComposedPrompt` with:
   - Final prompt text (available to preview and recipe snapshot, withheld from logs via redaction).
   - Per-layer input hashes.
   - Token resolution log.
   - Composition hash.

The frozen prompt evidence contains the final prompt or sufficient exact composition inputs to deterministically reproduce it — not only `final_hash`. This evidence belongs in `ResolutionPreview` / `GenerationRecipeSnapshot`, not in the published `WorkspacePromptPolicyVersion`.

### 4.3 Data Sources

#### DataSourceDefinition/Version

Content with adapter-specific config schema:
```
{
  "adapter_kind": "manual|shopify|mock",
  "shop_domain": "...",       // shopify only
  "api_version": "...",       // shopify only
  "capabilities": ["..."],
  "market_context": ["US", "DE"],
  "config": { ... },          // validated against allowlisted, versioned schema per adapter_kind
  "config_schema_version": "...",
  "execution_mode": "real|mock"
}
```

Secret detection is not reliable on arbitrary JSON. Each `adapter_kind` has an allowlisted, versioned config schema that rejects unknown fields. Key-name or entropy scanning is additional defense only.

No implicit "current default" source. Resolution without an explicitly selected DataSource → error. Not every adapter requires a credential — controlled Mock DataSource normally has no `CredentialBinding`.

#### CredentialBinding (`datasources.py`)

`bind_credential(context, data_source_entry_id, credential_version_id, *, idempotency_key, request_fingerprint)`:

1. Owner-only.
2. Validate both entries exist and are active in the same Workspace.
3. Previous active binding → `superseded`.
4. Create new binding with `revision = previous_max + 1`, `state = active`.
5. Concurrent bindings: the partial unique index ensures at most one active binding. Racing `bind_credential` calls result in a stable domain conflict (`LibraryConflict`), not a leaked `IntegrityError`.

Credential rotation: new `CredentialVersion` + new binding. `DataSourceVersion` unchanged. Historical `TruthPackage` not invalidated.

#### ManualFactSetVersion

Content: discriminated union by `fact_type`.

```
{
  "data_source_entry_id": "...",   // explicit link to the manual DataSource
  "facts": [
    {
      "fact_id": "stable-uuid",
      "fact_type": "price",
      "offer_version_id": "...",
      "country_code": "US",        // uppercase ISO 3166-1 alpha-2
      "amount": "29.99",           // canonical decimal string
      "currency": "USD",           // uppercase ISO 4217
      "valid_from": "2026-09-01",
      "valid_to": "2026-12-31",    // required; temporary truth without expiry is forbidden
      "evidence_source": "...",
      "evidence_note": "..."
    },
    {
      "fact_id": "stable-uuid",
      "fact_type": "shipping_availability",
      "offer_version_id": "...",
      "country_code": "DE",
      "available": true,
      "estimated_days_min": 3,
      "estimated_days_max": 7,
      "valid_from": "2026-09-01",
      "valid_to": "2026-12-31",
      "evidence_source": "...",
      "evidence_note": "..."
    }
  ]
}
```

Validation:
- `amount` parsed as `Decimal` — reject JSON floats, `NaN`, `Inf`.
- `currency` must be uppercase ISO 4217.
- `country_code` must be uppercase ISO 3166-1 alpha-2.
- Each fact has stable `fact_id`. Both `valid_from` and `valid_to` required.
- Discriminated by `fact_type`: price requires `amount`/`currency`; shipping requires different typed value shape.
- Missing currency, ambiguous scope, unmappable rows → line-level blocking errors in `ValidationReport`.
- `data_source_entry_id` explicitly identifies which manual DataSource this fact set serves. Resolution rules select a DataSource entry, and the binding identifies the applicable `ManualFactSetVersion`.

#### TruthResolutionPolicyVersion

Content:
```
{
  "rules": [
    {
      "fact_type": "price",
      "source_priority": ["ds-entry-id-1", "ds-entry-id-2"],
      "allow_fallback": false,
      "max_staleness_seconds": 86400,
      "on_failure": "block"
    }
  ]
}
```

Validation:
- One rule per `fact_type` (no duplicates).
- `source_priority` must be nonempty with unique entries.
- All referenced `data_source_entry_id`s must exist and be active in the same Workspace.
- `max_staleness_seconds` ≥ 0.
- `on_failure` restricted by `fact_type`: price cannot use `degrade` (which would preserve source-market truth for a missing price).

### 4.4 Resolution Preview

#### `resolve_preview(context, generation_draft_id, draft_revision, *, idempotency_key, request_fingerprint)`

In `preview.py`. Steps:

1. **Load and lock** generation draft. Verify `draft_revision` matches current revision.

2. **Resolve all versions** with selector provenance. For each dependency:
   - `latest` selector → resolve to current `effective` version.
   - `pinned` selector → resolve to the specified version; permits `effective` or `superseded` (if eligible); rejects `withdrawn` or `blocked`.
   - Record `{selector_kind, entry_id, resolved_version_id}` for every selection.

3. **Evaluate constraints** — run the bounded constraint solver on the resolved `DimensionSchemaVersion`. Produce planned RunItems and structured `SkipReason` records.

4. **Compose prompt** — call `compose_prompt` with resolved versions. Produces deterministic prompt with per-layer hashes.

5. **Assess provider capabilities** — check reference image count/size limits against provider defaults (stubbed capability matrix with configurable defaults until #21). Produce pruning decisions with a `decision_hash`. No silent truncation; if pruning is needed, return explicit pruning plan. Submission must carry and acknowledge `decision_hash`.

6. **Produce cost quote** — call `CostQuoteEngine` from #18. Mock execution: `payable_max_cost = "0"`, simulated estimate stored separately.

7. **Check eligibility** — verify rights, entitlements, credential binding status. Hash binding revision/eligibility evidence (not credential secrets).

8. **Build canonical snapshot** — assemble into a single `snapshot_json` with a `snapshot_schema_version` domain prefix:
   ```
   {
     "_schema": "resolution_preview/20.1",
     "resolved_versions": {
       "product": {"selector": "latest", "entry_id": "...", "version_id": "...", "content_hash": "..."},
       "offer": {...},
       "dimension_schema": {...},
       "person": {...},
       "prompt_policy": {...},
       "platform_baseline": {...},
       "data_sources": [{...}],
       "truth_resolution_policy": {...},
       "reference_groups": [{...}],
       "reference_assets": [{...}]
     },
     "constraint_results": {
       "valid_run_items": [...],
       "skip_reasons": [{"combination": {...}, "violations": [...], "primary_reason": "..."}]
     },
     "prompt_assembly": {
       "final_prompt": "...",
       "layer_hashes": {"baseline": "...", "policy": "...", "run_inputs": "..."},
       "token_resolution_log": [...],
       "composition_hash": "..."
     },
     "reference_choices": [
       {"run_item_index": 0, "refs": [{"ref_version_id": "...", "purpose": "...", "priority": 0}]}
     ],
     "provider_decisions": {
       "provider": "...", "model": "...", "params": {...},
       "capability_version": "...",
       "pruning": {"needed": false, "decision_hash": "..."}
     },
     "credential_evidence": {"binding_revision": 3, "eligible": true},
     "exclusions": [...],
     "cost_quote": {
       "payable_max_cost": "1.50",
       "currency": "USD",
       "simulated_estimate": null,
       "expires_at": "..."
     },
     "eligibility": {"overall": "eligible", "checks": [...]},
     "confirmation_decisions": [...]
   }
   ```

9. **Compute hashes**:
   - `recipe_input_hash = canonical_hash(snapshot excluding time-bound fields)` — excludes quote expiry, eligibility evidence, binding revision, confirmation decisions.
   - `preview_hash = canonical_hash(snapshot_json)` — includes everything.
   - Canonicalization sorts combinations, references, findings, and mappings deterministically.

10. **Compute `valid_until`** — `min(quote expiry, rights validity, fact validity, entitlement evidence expiry, and all other time-limited dependencies)`.

11. **Persist** — write to `resolution_previews`. Idempotent: same key replays original.

#### Invalidation

Selector-aware:
- Publishing a new version of an entry → invalidates previews that used `latest` selector for that entry. Does NOT invalidate previews that used `pinned` selector for the (now superseded) version, as long as it remains eligible.
- Withdrawal of any referenced version → always invalidates regardless of selector.
- Credential binding change → invalidates previews referencing that data source.
- Provider capability version change → invalidates only dependent previews.
- Unrelated publication → does not invalidate.
- Expiry of `valid_until` → preview becomes unusable without needing an explicit invalidation event.

Invalidation writes are an optimization. Submission-time transactional revalidation is authoritative.

#### Submission

Submission carries `preview_id` + `preview_hash` + `decision_hash` (if pruning). Server re-verifies all versions, eligibility, rights, entitlements, credential binding in the same transaction. Stale or invalidated preview → reject with reason.

One-time submission linkage:
- Retry with same `Idempotency-Key` → replay the original run creation.
- New submission intent using an already-consumed preview → reject.
- `submitted_run_id` and `submitted_at` set atomically with `GenerationRun` creation.

### 4.5 Dependency Graph & Impact Analysis

The existing `extract_dependencies` in `contracts.py` and `library_dependencies` table must be extended for the new entry types. New dependency relations:

| Relation | From → To |
|---|---|
| `schema_template` | DimensionSchemaVersion → DimensionTemplateVersion (provenance) |
| `schema_product` | DimensionSchemaVersion → ProductVersion (ownership) |
| `policy_template` | WorkspacePromptPolicyVersion → DimensionTemplateVersion (allowed templates) |
| `mapping_data_source` | MarketSourceMappingVersion → DataSourceVersion |
| `fact_set_data_source` | ManualFactSetVersion → DataSourceVersion |
| `truth_policy_data_source` | TruthResolutionPolicyVersion → DataSourceVersion |
| `preview_product` | ResolutionPreview → ProductVersion |
| `preview_offer` | ResolutionPreview → OfferVersion |
| `preview_schema` | ResolutionPreview → DimensionSchemaVersion |
| `preview_person` | ResolutionPreview → PersonVersion |
| `preview_policy` | ResolutionPreview → WorkspacePromptPolicyVersion |
| `preview_baseline` | ResolutionPreview → PlatformPromptBaselineVersion |
| `preview_data_source` | ResolutionPreview → DataSourceVersion |

Withdraw propagation: when any of these source versions is withdrawn, the `DependencyIndex` identifies downstream objects to project as `warning` or `blocked`. Impact analysis is shown before publish, withdraw, and archive.

### 4.6 Constraint Evaluation Call Sites

`evaluate_constraints` is used at two distinct call sites:

1. **Template/schema validation** (at publish time): verifies satisfiability — checks that at least one valid combination exists. Does not expand combinations; uses the bounded solver.

2. **ResolutionPreview** (at preview time): given a specific DimensionSchemaVersion and the generation intent, expands planned RunItems and produces SkipReasons for excluded combinations. Subject to the hard combination limit.

The function signature supports both: `evaluate_constraints(schema_content, planned_items=None)`. When `planned_items` is None, it performs satisfiability-only analysis. When provided, it expands and filters.

## 5. Authorization & MFA Step-Up

### 5.1 Role Boundaries

- **Member** may publish: `dimension_template`, `dimension_schema`.
- **Owner** required to publish: `prompt_policy`, `data_source`, `manual_fact_set`, `truth_resolution_policy`, `offer` (existing).
- **Platform operator** required for: `prompt_baselines` (separate authorization seam, outside Workspace RLS).

### 5.2 MFA Step-Up Proof

High-risk publications (Owner-only types, credential binding, withdrawal) require a verified, action-bound step-up proof:

```
{
  "proof_id": "unique-id",
  "workspace_id": "...",
  "actor_id": "...",
  "command": "publish|withdraw|bind_credential",
  "content_hash_or_revision": "...",   // bound to exact Draft revision or content hash
  "verified_at": "ISO-datetime",
  "expires_at": "ISO-datetime",        // freshness window
  "replay_nonce": "unique-nonce"       // one-time use
}
```

Validation rejects: missing proof, expired proof, wrong Workspace, wrong action, wrong content hash/revision, replayed nonce. Second confirmation is bound to the exact Draft revision/content hash — a concurrent edit invalidates the proof.

### 5.3 Audit Attribution

All commands record dual-actor attribution:
- `initiator_id` + `initiator_role` (the human who requested the action).
- `executor` (the service or Worker that performed it).
- `request_id`, `trace_id`, `authorization_epoch`.

Worker actions preserve the original human initiator; the Worker is recorded as executor only.

## 6. Testing Strategy

### 6.1 Test Infrastructure

- Move `LibraryFixtures` into `tests/support/library_fixtures.py` (non-test support module) to avoid cross-test imports and environment-order coupling.
- Add `hypothesis` to test dependencies.
- Test files mirror domain split: `test_templates.py`, `test_prompt_policy.py`, `test_datasources.py`, `test_resolution_preview.py`.

### 6.2 SQLite Suite (Fast Command Tests)

All command tests run against in-process SQLite via `LibraryFixtures`.

#### 6.2.1 Dimension Templates (`test_templates.py`)

**Property tests (Hypothesis):**
- Constraint satisfiability solver: random constraint graphs (≤5 dims × ≤5 values), verify solver agrees with brute-force on small sets.
- Cycle detection: authored `requires` cycles → blocking Finding (distinct from satisfiability).
- SkipReason: may cite multiple violated constraints. Output is deterministic primary reason plus `all_violations` list.
- Combination bound: exceeding hard limit → bounded domain failure, no quote/reservation/preview persisted.
- CreativeBrief: valid briefs pass against pinned Schema version; invalid → blocking. Schema version hash frozen in published content.

**Apply/reapply:**
- Apply deep-copies with new product-local IDs; source template unchanged.
- Provenance fields preserved: `derived_from_template_version_id`, `source_dimension_id`, `source_value_id`.
- Publishing new template version does not mutate applied schemas or published DimensionSchemaVersions.
- `product_entry_id` immutable across versions of same entry → blocking error on mismatch.
- Reapply three-way diff: template-only, local-only, and conflicts correctly classified. Local IDs preserved for retained dimensions/values.
- Failed apply leaves no orphan schema Entry (atomic).
- Apply idempotency: same key replays; same key + different fingerprint → rejected.
- Apply with existing Draft on the schema entry → conflict.

**Negative/isolation:**
- Apply with nonexistent/archived `product_entry_id` → error.
- Cross-workspace template → error.
- Concept key from another workspace → error.

#### 6.2.2 Prompt Policy (`test_prompt_policy.py`)

**Composition:**
- Same inputs → identical `ComposedPrompt` (final text, hashes, composition hash). Inject fixed clock.
- Token substitution single-pass: `{{token}}` in substituted runtime values remains literal.
- Recursive token definition in authoring (token A references `{{B}}`) → blocked at publish.
- Unknown token → blocking. Missing required token → blocking.
- Platform baseline clauses first; Workspace second; run inputs third. Deterministic order.

**Locked phrases:**
- Removing protected phrase during save → phrase restored, revision incremented, warning Finding.
- Publish with unacknowledged warning → blocked.
- Publish with acknowledged warning → succeeds.

**Platform baseline:**
- Unique `version_no`, at most one effective version.
- Withdraw persists outbox/domain event + impact records.
- Platform authorization separate from Workspace RLS.
- Platform actors can mutate baselines; Workspace roles cannot.

**Evidence:**
- Exact prompt evidence in ResolutionPreview/GenerationRecipeSnapshot tests, not published policy tests.
- Redaction: execute command with sentinel prompt content, capture observable logs/audit/export, assert sentinel never appears (no direct `_redact()` testing).

#### 6.2.3 Data Sources (`test_datasources.py`)

**DataSource lifecycle:**
- Create → validate → publish with adapter-specific config schema.
- Unknown fields in config → rejected (per-adapter allowlisted schema).
- No implicit default: resolution without explicit DataSource selection → error.
- Controlled Mock normally has no CredentialBinding.

**CredentialBinding:**
- `bind_credential` creates active binding; previous → superseded.
- Concurrent bindings: racing calls → stable domain conflict, not leaked IntegrityError.
- Credential rotation does not create new DataSourceVersion.
- Binding state `active`/`superseded` only.

**ManualFactSet:**
- `"29.99"` (string) passes; `29.99` (float) → blocking. `"NaN"`/`"Inf"` → blocking.
- Currency uppercase. `country_code` uppercase.
- Both `valid_from` and `valid_to` required. Each fact has stable `fact_id`.
- Discriminated union: price requires amount/currency; shipping requires different shape.
- `data_source_entry_id` explicitly identifies which manual DataSource this fact set serves.

**TruthResolutionPolicy:**
- One rule per `fact_type`. Nonempty, unique source lists. Nonnegative staleness.
- `on_failure` restricted by fact_type: price → cannot use `degrade`.
- Referenced DataSource entries must exist and be active.

**Mode isolation:**
- Mock DataSource + Mock CredentialBinding cannot produce real evidence.

#### 6.2.4 Resolution Preview (`test_resolution_preview.py`)

**Deterministic hashing (inject fixed clock, quote adapter, capability catalog):**
- Same recipe inputs → same `recipe_input_hash`.
- Same complete snapshot including fixed time-bound evidence → same `preview_hash`.
- Canonicalization sorts combinations, references, findings, mappings. Includes snapshot schema version prefix.

**Selector behavior:**
- `latest` → current effective version.
- `pinned` → permits effective or superseded (if eligible); rejects withdrawn/blocked.
- Every selection records `{selector_kind, entry_id, resolved_version_id}`.

**Invalidation with selector provenance:**
- New version published → invalidates `latest` previews; does NOT invalidate `pinned` previews for superseded version.
- Withdrawal → always invalidates regardless of selector.
- Credential binding change → invalidates.
- Provider capability version change → invalidates only dependent previews.
- Unrelated publication → does NOT invalidate.
- Preview expiry without invalidation event → unusable at submission.
- Preview hash tampering → submission rejected.

**Submission:**
- Stale/invalidated → reject with reason.
- One-time: same Idempotency-Key → replay original run. Already-consumed preview with new intent → reject.
- Secrets and CredentialVersion IDs absent from recipe content.

**Cost & mock:**
- Mock: `payable_max_cost = "0"`, simulated estimate separate.
- `valid_until` = min of all time-limited dependencies.

**Provider pruning:**
- Pruning decision_hash. Submission must acknowledge.
- No silent truncation.

### 6.3 PostgreSQL Suite

Separate test suite for hosted-path invariants that SQLite cannot prove.

#### RLS and tenant isolation:
- `dimension_concept_keys`: workspace-scoped keys invisible to other workspaces.
- `datasource_credential_bindings`: cross-workspace binding → RLS rejection.
- `resolution_previews`: cross-workspace preview access → RLS rejection.

#### Constraints and indexes:
- Composite tenant foreign keys enforce workspace-scoped referential integrity.
- Partial unique index: two active `datasource_credential_bindings` for same data source → constraint violation.
- `prompt_baselines` partial unique index: two effective baselines → constraint violation.

#### Concurrency:
- Concurrent credential binding → exactly one active binding.
- Concurrent baseline publication → exactly one effective baseline.
- Preview invalidation racing with submission → cannot create a stale run or reservation.

#### Runtime-role permissions:
- Platform actors can mutate `prompt_baselines`; Workspace roles cannot.
- Domain database role cannot read/write `prompt_baselines` directly.

#### Migration:
- Upgrade/downgrade tests for both SQLite and PostgreSQL dialects.
- Downgrade safety when new immutable rows already exist.
- Update protected-table inventory in `test_postgres_rls.py`.

### 6.4 Transaction & Concurrency Evidence

Every new mutating command must cover:

1. Same Idempotency-Key + fingerprint → replays original result.
2. Same key + different fingerprint → rejected.
3. Stale expected revision → rejected.
4. Blocking validation creates no partial version or dependency rows.
5. Failed Apply leaves no orphan schema Entry.
6. Concurrent bindings leave exactly one active binding.
7. Concurrent baseline publication leaves exactly one effective baseline.
8. Preview invalidation racing with submission → no stale run or reservation.

### 6.5 Authorization Evidence

1. Missing, expired, wrong-Workspace, wrong-action, and replayed MFA step-up proof → rejected.
2. Second confirmation bound to exact Draft revision/content hash.
3. Initiator/executor audit attribution for both request and Worker actions.
4. Member positive publication for `dimension_template` and `dimension_schema`.
5. Owner-only publication and withdrawal for the five protected types.
6. Platform operator positive/negative tests for baselines.

## 7. Migration

Single new Alembic migration. Creates:
- `dimension_concept_keys` table
- `prompt_baselines` table
- `datasource_credential_bindings` table
- `resolution_previews` table

Updates:
- `library_entries` CHECK constraint on `entry_type` to include new types.
- `library_versions` CHECK constraint on `entry_type` to include new types.
- Protected-table inventory for RLS.

Legacy `DimensionSchemaRecord`/`DimensionRecord`/`DimensionValueRecord` remain untouched. Migration includes downgrade path.

## 8. Excluded

- Shopify live access (adapter work after #9/customer approval).
- Provider execution and generated Asset behavior (#21).
- Real provider capability matrix (#21 fills in; stubbed with configurable defaults here).
- Multi-person real-time editing.
- Cross-workspace template marketplace.
