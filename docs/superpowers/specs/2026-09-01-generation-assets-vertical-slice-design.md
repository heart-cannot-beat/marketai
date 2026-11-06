# Issue #21: Generation → Assets Vertical Slice — Design Specification

**Date:** 2026-09-01
**Issue:** [#21 — Deliver the production Generation to Assets vertical slice](https://github.com/Raybbish/marketai/issues/21)
**Status:** Revised design, pending approval and implementation plan
**Blocked by:** #18 (closed), #20 (closed)
**Directly blocks:** #22, #24, #25, #27; **transitively blocks:** #23

## Overview

This specification defines the entity model, lifecycle state machines, HTTP/Worker interfaces, execution-mode isolation, provenance model, migration strategy, and phased automated evidence for the Generation → Assets vertical slice. It evolves the existing persistence and Job/Operation infrastructure into immutable frozen-input execution with RunItem as the sole execution identity, ProviderAttempt as append-only external-call evidence, and AssetVersion as immutable output.

The design spans five sections:

1. **Entity model and identity hierarchy** — durable identities, new/modified tables, atomic submission, and legacy migration strategy.
2. **Lifecycle and state machines** — Run/RunItem/ProviderAttempt/QC projections, cancellation, retry, resume, reuse, reconciliation, and cost exposure.
3. **HTTP command surface, response projections, and Worker handlers** — private FastAPI routes, idempotency, Job dispatch, and legacy containment.
4. **Controlled Mock, execution-mode isolation, and provenance** — three-mode contract, GenerationExecutionPolicy, export isolation, and downstream safeguards.
5. **Migration strategy, phased rollout, and automated evidence** — production deployment sequence, implementation capability phases, database invariants, and completion gates.

---

# Section 1: Entity Model and Identity Hierarchy

This section defines the durable identities for Generation → Assets. It evolves the existing persistence and Job/Operation infrastructure while making `RunItem`, rather than `Asset`, the execution identity.

## Modeling rules

- Every domain row is Workspace-scoped and protected by PostgreSQL RLS.
- Cross-table references use composite `(workspace_id, target_id)` foreign keys.
- Mutable state is an explicitly identified projection updated with optimistic compare-and-swap (CAS).
- Historical facts, external-call evidence, QC evidence, reviews, and cost evidence are append-only.
- `GenerationDraft` stores selection intent. `ResolutionPreview` resolves that intent. `GenerationRecipeSnapshot` is the only business-content input used by Workers.
- `Asset` and `AssetVersion` represent outputs, never queued or running work.

## New tables and evolved ProviderAttempt identity

### `generation_drafts`

**Role:** Mutable Workspace-scoped intent before preview and freeze.

Stores:

- latest/pinned selectors for Product, Offer, Dimension Schema, Person, Reference, Prompt Policy, and Data Source versions;
- Provider/model preferences, format, generation intent, exclusions, and combination strategy;
- regeneration intent (`new_generation`, `same_recipe`, `new_seed`, `edit_prompt`, or `quality_feedback`);
- optional source `AssetVersion` and source recipe identity for regeneration;
- `revision` for optimistic locking.

The draft never contains authoritative resolved execution inputs and is never read by a Worker.

**Mutability:** Mutable through CAS commands using `expected_revision`.

### `resolution_preview_confirmations`

**Role:** Append-only evidence that an actor explicitly confirmed a particular immutable preview and cost quote.

Stores:

- `id`, `workspace_id`, `preview_id`, and `preview_hash`;
- `actor_id`, authorization context, and `confirmed_at`;
- request fingerprint and the immutable quote/hash presented to the actor;
- quoted maximum provider and Workspace-budget amounts, currencies, pricing version, and exchange-rate evidence where applicable.

It does **not** store `consumed_run_id`. The initial Run references its creation confirmation, while later recovery commands reference their own confirmation through `generation_run_commands`, avoiding a circular foreign key.

The submission command uses the platform idempotency contract:

- same `Idempotency-Key` plus the same request fingerprint returns the existing Operation and Run;
- same key plus a different fingerprint returns an idempotency conflict;
- `preview_hash` alone is not an idempotency key;
- a deliberate new-seed or force intent requires a new preview and a new explicit submission intent.

**Mutability:** Append-only. Unique `(workspace_id, preview_id)` for the confirmed-preview submission path.

### `generation_recipe_snapshots`

**Role:** First-class immutable execution content object.

Stores:

- UUID `id` and `workspace_id`;
- `canonicalization_version`, `recipe_hash`, and canonical snapshot JSON;
- resolved Product, Offer, Dimension Schema, Person, Reference, Prompt, Data Source, capability, credential-binding eligibility, mode, exclusion, and cost-contract evidence;
- the final prompt and its layered input hashes;
- provider/model/capability decisions, format, seed strategy, price rules, and execution mode;
- `created_at` and creation provenance without secrets.

Unique constraint:

```text
(workspace_id, canonicalization_version, recipe_hash)
```

The snapshot is a Workspace-scoped content object. It is not exclusively owned by a confirmation and may be reused by multiple Runs when their explicit intent permits it. Database protections reject update/delete outside Workspace erasure policy.

**Mutability:** Immutable, write-once.

### `run_items`

**Role:** Sole queued/running execution identity within a `GenerationRun`.

Stores:

- `id`, `workspace_id`, `generation_run_id`, ordinal, combination identity, stable dimension signature, tags, and variant index;
- requested seed, effective seed, seed availability/source, and seed-unavailable reason;
- semantic fingerprint and explicit intent kind;
- optional source `AssetVersion`/RunItem for regeneration or reuse;
- current state projection, error summary, timestamps, and `revision` for CAS;
- optional `legacy_asset_id` used only for deterministic migration provenance.

Jobs use:

```text
resource_type = 'run_item'
resource_id   = run_items.id
```

Provider retry, crash recovery, reuse, cancellation, and QC feedback never turn an `Asset` into a task row.

**Mutability:** State projection is mutable through CAS. Attempt, Job, audit, and output history remain append-only.

### `provider_call_attempts` (evolved ProviderAttempt header)

**Role:** Existing #18 identity for one planned external call, deepened into the #21 ProviderAttempt header. #21 does not create a parallel `provider_attempts` table or a second attempt source of truth.

The expand migration retains each existing `provider_call_attempts.id` and adds:

- nullable `run_item_id`, required for new generation and visual-QC attempts;
- `attempt_number` as the domain name for the existing `attempt_no` value;
- `step_kind` such as `generation` or `visual_qc`;
- optional input `asset_version_id` for QC/edit steps;
- immutable model/API-version, request-evidence reference, and redacted request hash fields not already present;
- mode-aware nullable `spend_reservation_id`, because `mock` and the no-charge `sandbox` path create no payable reservation.

Provider, execution mode, provider idempotency key, and the actual CredentialVersion used remain on this same identity. For `byok`, the Worker resolves the current active binding immediately before the external call and records that exact CredentialVersion. `sandbox` follows the selected no-charge Adapter's binding rule. `mock` records no CredentialVersion. No attempt silently falls back to another mode.

New-path identity/request fields are immutable after preparation. Existing mutable dispatch/outcome columns remain compatibility projections during expand/drain only; new-path writers append events and advance the projection below. M3 proves equivalence, stops writing the compatibility columns, and M4 may remove them. Historical IDs are never copied into a second attempt table.

**Mutability:** Identity and prepared request evidence are write-once; legacy mutable columns are transitional compatibility projections only.

### `provider_attempt_events`

**Role:** Append-only evidence stream for a ProviderAttempt.

Event types include:

- `dispatch_started` — committed before crossing the external-call seam;
- `response_received`;
- `failure_classified`;
- `usage_reported`;
- `certainty_resolved`;
- `reconciled`;
- `legacy_state_imported` — deterministic migration evidence derived only from an existing #18 attempt row; it never upgrades an unknown historical seam into a conclusive dispatch fact.

Each event stores:

- `workspace_id`, `provider_call_attempt_id`, monotonic sequence number, event type, and timestamp;
- event idempotency key;
- typed payload and redacted evidence/object references with hashes;
- system executor and trace/request attribution.

Unique constraints prevent duplicate sequence numbers and duplicate semantic events. Database protections prevent update/delete outside Workspace erasure policy.

**Mutability:** Append-only.

### `provider_attempt_projections`

**Role:** Rebuildable current-state projection for efficient coordination and querying.

References `provider_call_attempts.id` and stores current dispatch certainty, outcome, summarized usage, settlement/reconciliation state, last applied event sequence, and CAS revision.

Appending an event and advancing the projection occur in one transaction. A rebuild from events must reproduce the same projection.

The three physical tables are exposed to callers and tests through one deep ProviderAttempt module interface. Callers do not coordinate headers, events, and projections themselves.

**Mutability:** Mutable, rebuildable CAS projection.

### `run_item_asset_version_bindings`

**Role:** Append-only association between a RunItem and an AssetVersion used as its result.

Stores:

- `run_item_id` and `asset_version_id`;
- relationship kind: `produced` or `reused`;
- optional producing ProviderAttempt;
- binding timestamp and provenance.

This association allows:

- one RunItem to retain multiple immutable candidate versions produced during bounded QC feedback;
- a later RunItem to reuse an existing successful AssetVersion without rewriting that version's producer;
- projections to identify the selected/current usable result without losing historical bindings.

**Mutability:** Append-only. Current/selected result is a rebuildable RunItem projection, not a rewritten binding.

### `asset_qc_attempts`

**Role:** Identity and rebuildable current-state projection for one automated QC evaluation round on an exact AssetVersion.

Stores:

- `id`, `workspace_id`, `asset_version_id`, `run_item_id`, and `feedback_round`;
- QC policy/rule-set versions and immutable input hash;
- optional ProviderAttempt for external visual QC;
- execution mode, current state, last applied result/event sequence, and CAS revision;
- bounded-feedback summary and redacted evidence references.

An AssetQCAttempt may block approval eligibility but never creates an `Approved` human review.

The identity, input hash, policy versions, and subject AssetVersion are immutable. State transitions update only the CAS projection and append audit evidence; individual check facts remain append-only in `asset_qc_check_results`. In-flight external-call certainty is owned by ProviderAttempt.

**Mutability:** Mutable, rebuildable CAS state projection over immutable identity/input plus append-only results and audit evidence.

### `asset_qc_check_results`

**Role:** Individual rule results belonging to an AssetQCAttempt.

Stores rule identity/version, check type, severity, outcome, evidence, and feedback detail. Blocking must-match failures remain attributable to their exact rule and AssetVersion.

**Mutability:** Append-only.

### `generation_run_commands`

**Role:** Append-only command history linking one GenerationRun to its creation, retry, resume, cancel, and reconciliation Operations.

Stores:

- `id`, `workspace_id`, `generation_run_id`, and unique `operation_id`;
- command kind (`submit`, `retry_failed_items`, `resume_paused_items`, `cancel`, or `reconcile`);
- immutable target-selection JSON/hash and expected Run revision;
- `preview_confirmation_id`, required for `submit`, `retry_failed_items`, and `resume_paused_items`, and absent for commands such as cancel/reconcile that do not confirm a Preview;
- actor/authorization attribution and `created_at`.

The creation command references the same confirmation as `GenerationRun.creation_preview_confirmation_id`. Recovery commands never overwrite it.

**Mutability:** Append-only.

### `export_packages`

**Role:** Durable result identity for an asynchronous ZIP/Manifest export.

Stores:

- `id`, `workspace_id`, creation `operation_id`, actor attribution, and immutable export-selector snapshot;
- exact selected AssetVersion/business-reference identities and contextual review/verdict evidence frozen for the export;
- state projection, revision, object-store committed reference, byte size, ZIP hash, Manifest hash, counts, and failure evidence;
- one frozen generation execution mode and retention timestamps without signed URLs or secrets.

The ExportPackage outlives any short-lived DownloadIntent. A later authorized command resolves the package's internal committed object reference and issues a fresh DownloadIntent.

**Mutability:** Selector and provenance are immutable; processing state is a CAS projection; completed hashes/object identity are write-once.

## Modified tables

### `resolution_previews`

Extend the existing immutable preview with a purpose discriminator:

- `initial_generation` references a GenerationDraft and freezes executable/SkipReason entries;
- `run_recovery` references a GenerationRun, command kind, exact target-item selection, current eligibility, remaining confirmed exposure, and maximum additional cost.

A recovery preview never changes recipe content. It only proves which existing items may retry/resume and what new capacity/cost confirmation is required. Both kinds use the same preview hash, expiry, dependency/capability/credential-binding checks, and append-only confirmation evidence.

### `generation_runs`

Add:

- `recipe_snapshot_id` → `generation_recipe_snapshots` (`NOT NULL` after truthful backfill, `RESTRICT`);
- `creation_preview_confirmation_id` → `resolution_preview_confirmations` (unique, `RESTRICT`, required for new confirmed-preview submissions);
- `creation_operation_id` → `operation_records` (`RESTRICT`, required for new submissions);
- `submission_kind` with at least `confirmed_preview` and `legacy_migrated`;
- explicit state/count projections defined in Section 2;
- `revision` for CAS.

Conditional constraints:

- `confirmed_preview` requires both confirmation and Operation;
- `legacy_migrated` must not claim an explicit user confirmation that did not occur;
- every Run references a recipe snapshot, including a clearly marked incomplete `legacy-v0` snapshot where historical evidence permits deterministic construction.

Provider, model, aspect-ratio, image-size, prompt, and related execution inputs move to the snapshot as their source of truth. Existing columns are removed only in the contract phase after backfill and after no implementation path reads them.

### `assets`

`Asset` remains the stable output identity. No RunItem or Job state is added to it. Existing `run_id` is treated as immutable origin-run provenance for legacy compatibility, not current task state.

An Asset is created when the first provider output is durably persisted—not when QC or human review succeeds.

### `asset_versions`

Add:

- nullable `producing_run_item_id` → `run_items` (`RESTRICT`);
- nullable `provider_attempt_id` → `provider_call_attempts` (`RESTRICT`);
- explicit origin/provenance kind;
- requested/effective seed evidence or seed-unavailable reason where generation-origin content needs a local manifest projection.

Generation-origin versions require producing RunItem and ProviderAttempt identities. The fields remain legitimately nullable for manual, imported, localized, and historical versions; nullability is not described only as backward compatibility.

`GenerationRecipeSnapshot`, RunItem fingerprint, ProviderAttempt, and binding references remain the normalized provenance. Any copied manifest fields on AssetVersion are immutable integrity projections validated at insertion, not competing sources of truth.

Provider output persistence atomically creates the Asset/AssetVersion, its producing binding, content hash evidence, and corresponding UsageEvent. QC happens afterward, so a QC-failed item still retains every generated AssetVersion and failure record.

### `quality_reviews`

Human reviews become exact-AssetVersion, append-only history.

Add:

- nullable `asset_version_id` for migration expansion, required for all new reviews;
- `legacy_unscoped` flag;
- nullable `legacy_asset_id` retained only for ambiguous historical reviews;
- review value (`approved`, `needs_changes`, or `rejected`), reason, actor, attribution, and timestamp.

Constraints enforce exactly one provenance form:

```text
new/version-scoped: asset_version_id IS NOT NULL AND legacy_unscoped = false
legacy/unscoped:     asset_version_id IS NULL     AND legacy_unscoped = true
                     AND legacy_asset_id IS NOT NULL
```

Legacy-unscoped reviews are preserved for history but excluded from current AssetVersion review projections. A new `approved` review is rejected while blocking QC failures make that exact version ineligible.

### `spend_reservations` and `provider_cost_ledger`

Issue #18 remains the sole owner of payable exposure and provider-cost facts. #21 extends its authoritative tables rather than writing new facts to legacy `cost_ledger`.

Add to `spend_reservations`:

- nullable `run_item_id`, required for new paid generation/visual-QC reservations;
- `preview_confirmation_id` and/or `recipe_snapshot_id` for frozen Quote provenance;
- the existing operation/job/semantic-key linkage remains authoritative.

Add to append-only `provider_cost_ledger`:

- `run_item_id` for generation cost evidence;
- physical `provider_call_attempt_id` → `provider_call_attempts` where an external attempt exists (exposed as ProviderAttempt identity in domain projections);
- confirmation/snapshot references for Quote provenance;
- reconciliation-evidence and actor attribution where an Adjustment requires them.

The #18 entry types (`quote`, `reservation`, `release`, `reported_cost`, `estimated_settlement`, `adjustment`), amounts, currencies, exchange-rate evidence, direction semantics, SpendReservation linkage, and price-rule evidence remain canonical. Constraints additionally require:

- Quote references the frozen confirmation/snapshot and, once materialized at submit, its RunItem;
- Reservation/Release reference the RunItem and exact SpendReservation;
- Reported Cost and Estimated Settlement reference both RunItem and ProviderAttempt;
- Adjustment references the corrected entry and reconciliation evidence and explicitly increases or decreases the prior result.

`mock` and no-charge `sandbox` create no payable SpendReservation and no payable `provider_cost_ledger` entry. A simulated Mock estimate belongs to separate mode/scenario evidence and cannot aggregate into provider cost, Spend Guardrail, Billing, real usage, or Experiment evidence.

Legacy `cost_ledger` remains untouched. The #18 read-only compatibility bridge continues to expose its `reserved`, `released`, and `actual` rows exactly once, mapping configuration-derived legacy `actual` to estimated settlement without copying those rows into `provider_cost_ledger`.

## Deterministic combination planning and coverage sampling

GenerationDraft carries requested planning intent; ResolutionPreview materializes the complete immutable plan; GenerationRecipeSnapshot copies that plan byte-for-byte. Workers never enumerate or resample combinations.

The versioned `combination_strategy` is a discriminated value:

```text
kind: full | coverage
planner_version
constraint_set_id
constraint_set_version
variants_per_combination

coverage-only:
  coverage_strength: 1 | 2
  maximum_selected_combinations
  tie_break_version
```

No planner default is introduced at submit time. All fields that affect membership, order, quote, Seed derivation, or fingerprints are present in the Preview hash.

### Canonical candidate universe

Planner v1:

1. sorts resolved dimensions by stable dimension identity and each dimension's values by immutable version identity;
2. forms the Cartesian product from the complete resolved value sets, never from a paginated API page;
3. evaluates the frozen, versioned constraint set in stable rule order;
4. assigns every rejected candidate a typed SkipReason before sampling;
5. orders feasible candidates by canonical combination fingerprint.

For `full`, every feasible candidate is selected. If the selected count exceeds a frozen provider or policy maximum, Preview creation fails with `combination_limit_exceeded`; it never silently turns full planning into sampling.

For `coverage`, planner v1 constructs feasible coverage obligations:

- strength `1`: every feasible individual dimension-value identity;
- strength `2`: every feasible individual identity plus every feasible pair of identities from different dimensions.

It then applies deterministic greedy set cover: choose the candidate covering the largest number of still-uncovered obligations, break equal scores by canonical combination fingerprint, remove the obligations it covers, and repeat. If `maximum_selected_combinations` is reached before all feasible obligations are covered, Preview creation fails with `coverage_budget_insufficient` and reports the remaining obligation hashes; partial coverage is never presented as complete. Selected candidates retain greedy selection order with fingerprint as the stable secondary order. `variants_per_combination` expands only after combination selection, using `(selected_ordinal, variant_index)` for RunItem ordering and Seed derivation.

### SkipReason evidence

SkipReason is immutable typed evidence, not authoritative free text. Each entry contains:

```text
code
phase: constraint | capability | policy | sampling
planner_version
rule_id and rule_version when applicable
candidate_fingerprint or coverage_obligation_hash
redacted subject identities
stable detail fields
```

Required codes include `excluded_by_rule`, `capability_ineligible`, `policy_ineligible`, and `not_selected_by_sampling`. Planner-wide failures such as `combination_limit_exceeded` and `coverage_budget_insufficient` are typed Preview errors, not fake failed RunItems. A compact content-addressed partition may represent large nonselected candidate sets only when its canonical expansion and count are reproducible from the frozen inputs.

The Preview records candidate, feasible, selected, variant, and skipped counts; ordered selected fingerprints; coverage-obligation totals; covered/uncovered hashes; SkipReason partitions and hashes; and the complete planner/constraint versions. Submission creates exactly one RunItem per selected combination/variant and rejects zero executable RunItems. Replanning under another algorithm, constraint version, value set, limit, or coverage budget requires a new Preview.

## Corrected identity hierarchy

```text
GenerationDraft (mutable selector intent)
  └─ ResolutionPreview (immutable resolved content, time-limited)
       ├─ ResolutionPreviewConfirmation (actor + exact quote evidence)
       └─ canonical content → GenerationRecipeSnapshot (immutable content object)

Creation ResolutionPreviewConfirmation + GenerationRecipeSnapshot + Operation
  └─ GenerationRun (state projection)
       ├─ RunItem 1 (sole execution identity)
       │    ├─ ProviderAttempt 1..N
       │    │    ├─ ProviderAttemptEvent 0..N
       │    │    └─ ProviderAttemptProjection
       │    ├─ RunItemAssetVersionBinding 0..N
       │    │    └─ Asset
       │    │         └─ AssetVersion (immutable output)
       │    │              ├─ AssetQCAttempt 0..N
       │    │              │    └─ AssetQCCheckResult 1..N
       │    │              └─ QualityReview 0..N (human, version-scoped)
       │    └─ optional reused AssetVersion binding
       ├─ RunItem 2
       └─ RunItem N

GenerationRun
  ├─ GenerationRunCommand 1..N → Operation
  │    └─ optional recovery ResolutionPreviewConfirmation
  └─ ExportPackage 0..N → short-lived DownloadIntent created on demand
```

`GenerationRecipeSnapshot` is not exclusively owned by a confirmation. `ResolutionPreviewConfirmation` does not point back to its Run. The Run and append-only command records reference confirmations, so the graph has no circular foreign key.

## Legacy migration strategy

Section 5 is the single normative rollout definition: expand → cut over → drain → backfill → verify → constrain → later contract. This section defines only identity outcomes. Migration must preserve historical IDs and evidence, create deterministic `legacy-v0` snapshots and RunItems where truthfully possible, retain ambiguous reviews as `legacy_unscoped`, evolve `provider_call_attempts` in place, and keep the #18 legacy-cost compatibility bridge single-counted. It never invents confirmation, Seed, credential, Provider, usage, QC, or reported-cost evidence.

## Atomic submission interface

Expose one submission module interface, conceptually:

```text
submit_generation(
  tenant_context,
  preview_id,
  preview_hash,
  expected_draft_revision,
  idempotency_key
) -> { operation_id, generation_run_id }
```

The interface first uses the existing durable pending-idempotency-reservation protocol. The reservation binds Workspace, actor, command type, and normalized request fingerprint before the domain body runs. A concurrent/replayed caller waits for or returns the committed outcome; it never executes a second body.

The winning caller then runs one domain transaction that:

1. locks and verifies the pending idempotency reservation;
2. validates Workspace, actor, role, authorization epoch, entitlement lease, preview freshness/hash, dependencies, rights, credential-binding eligibility, provider capabilities, quote, and Spend Guardrail;
3. inserts the confirmation;
4. inserts or reuses the Workspace-scoped immutable recipe snapshot;
5. creates the Operation, Run, initial GenerationRunCommand, RunItems, dependency/audit evidence, and—for paid non-reused work—the required Quote/SpendReservation/provider-cost provenance, capacity, and Job records; no-charge `sandbox` and `mock` create Jobs/evidence without payable reservations;
6. finalizes the idempotency outcome with the Operation/Run identity and commits without making a Provider call.

If the winner crashes before finalization, the durable pending reservation remains the sole recovery point. Replay resumes/waits according to the platform protocol and cannot create a duplicate Run.

Exact replay returns the existing Operation and Run. A reused idempotency key with different intent fails closed. Deliberate new-seed, force, edited-prompt, or quality-feedback generation requires a newly previewed explicit intent.

The transactional interface hides table ordering, event/projection coordination, and migration compatibility from callers. Tests cross the same interface and assert persisted outcomes and invariants.

## Section 1 invariants

- A new GenerationRun exists only through the atomic submission interface with a fresh confirmed preview, immutable snapshot, Operation, and RunItems; non-reused paid work additionally requires atomic cost/capacity evidence and Jobs.
- A Worker reads business content only from `GenerationRecipeSnapshot`; it never re-resolves `latest` draft selectors.
- Full and coverage plans follow the frozen planner/constraint versions; submission copies the Preview's exact ordered plan and never replans it.
- `RunItem` is the execution identity. `Asset` and `AssetVersion` are output identities.
- ProviderAttempt request identity is immutable; event history is append-only; current attempt state is rebuildable.
- Crossing an external-call seam requires a persisted `dispatch_started` event and valid paid-call reservation where applicable.
- Worker loss after dispatch but before durable response evidence produces `uncertain` and reconciliation, never a blind paid retry.
- Durable provider output creates immutable AssetVersion evidence before QC. QC failure cannot erase the output.
- Automated QC cannot create a human `Approved` review. Blocking must-match failures prevent approval eligibility for that exact AssetVersion.
- Reuse links a RunItem to the existing successful AssetVersion; it never rewrites producer provenance or creates a duplicate charge.
- Canonical `byok`, `sandbox`, and `mock` evidence remains mode-isolated. Sandbox and Mock payable cost is zero; neither enters BYOK spend or experimental truth.
- Every cross-Workspace identifier is rejected by the module, composite foreign keys, and RLS.
- Historical records are preserved; migrations never fabricate explicit confirmation, Seed, usage, Provider response, or reported cost.

---

# Section 2: Lifecycle and State Machines

This section defines lifecycle ownership across GenerationRun, RunItem, Job, ProviderAttempt, AssetVersion QC, Operation, cancellation, retry, resume, reconciliation, and cost exposure.

The state machines are deliberately not one duplicated machine:

- **Operation** is the user-facing lifecycle of one asynchronous command.
- **GenerationRun** is a rebuildable projection over all of its RunItems.
- **RunItem** owns domain progress for one combination × variant execution intent.
- **Job** owns scheduling, leasing, heartbeat, and backoff only.
- **ProviderAttempt** owns external-call identity, dispatch certainty, response evidence, and reconciliation.
- **AssetQCAttempt** owns QC progress for one exact AssetVersion.
- **SpendReservation and Provider Cost Ledger** own payable exposure and settlement; the legacy ledger is compatibility evidence only.

Callers cross small command interfaces. They never coordinate these projections by directly updating rows.

## GenerationDraft lifecycle

GenerationDraft has a small formal state projection:

```text
active | archived
```

It is a mutable intent document only while `active`; every state or content change uses CAS revision.

Rules:

- Every edit requires `expected_revision` and increments `revision`.
- Archive is an explicit idempotent transition from `active` to `archived`; an archived Draft rejects edits and new Preview creation. There is no restore transition in #21.
- Every ResolutionPreview records the exact Draft revision it resolved.
- Editing the Draft does not mutate an existing Preview, Confirmation, Snapshot, Run, RunItem, or AssetVersion.
- Submission rejects a Preview whose recorded Draft revision or resolved dependencies no longer match.
- A Draft may produce multiple Previews and Runs over its lifetime, but one confirmed Preview can create at most one Run.
- The Draft is not consumed after submission and may be edited for a later generation cycle.
- Workers never read the Draft.

## Operation ownership

The initial submission creates `GenerationRun.creation_operation_id`. Later commands—retry, resume, cancel, or reconciliation requiring an explicit command—create new Operations whose result reference points to the same GenerationRun. Every command is linked through an append-only `GenerationRunCommand`; commands that confirm new exposure also reference their recovery ResolutionPreviewConfirmation.

Operations never reopen after reaching a terminal state. A later recovery Operation may reopen eligible RunItems and therefore change the current Run projection without rewriting the historical state of the creation Operation.

Operation state remains the contract-defined set:

```text
queued
running
partially_succeeded
succeeded
failed
cancelled
uncertain
```

Mapping rules:

- Run `queued` → active Operation `queued`.
- Run `running` or internal Run `paused` → active Operation `running`; detail exposes pause reasons and `requires_action`.
- Run `uncertain` → active Operation `uncertain`.
- Quiescent Run outcomes map to their corresponding Operation terminal outcome.
- Only reconciliation may transition an Operation from `uncertain` back to `running` or a terminal state.
- A user retry never reopens an uncertain Operation; it is rejected until certainty is resolved.

## GenerationRun projection

`submitting` is not a persisted Run state. The submission transaction either commits a complete `queued`/already-`reused` Run or leaves no Run.

### Run states

```text
queued
running
paused
uncertain
succeeded
partially_succeeded
failed
cancelled
```

`paused` is an internal Run projection. The public Operation remains `running` and exposes `requires_action=true` because the Operation contract has no `paused` state.

Cancellation is stored as command intent (`cancel_requested_at`, actor, reason, command Operation), not as an immediate overwrite of the Run outcome. While cancellation drains, the UI may render "cancelling" from the intent plus non-quiescent item counts.

### Projection precedence

Rebuild Run state using this exact precedence:

1. If any item is executable or in flight (`queued`, `preparing`, `retry_wait`, `awaiting_provider`, `processing_output`, `qc_pending`, or `feedback_pending`):
   - `queued` only when no item has ever started and every executable item is still queued;
   - otherwise `running`.
2. If no item is executable/in flight and any item is `uncertain`: `uncertain`.
3. If no item is executable/in flight/uncertain and any item is `paused`: `paused`.
4. If every item is `succeeded` or `reused`: `succeeded`.
5. If at least one item is `succeeded` or `reused` and every remaining item is `failed` or `cancelled`: `partially_succeeded`.
6. If every item is `cancelled`: `cancelled`.
7. Otherwise, when all items are quiescent and none succeeded/reused: `failed`.

An uncertain item does not force the whole Run to `uncertain` while siblings are still executing. A cancellation after some items succeeded produces `partially_succeeded`, not `cancelled`.

Submission rejects a generation with zero executable RunItems. Constraint exclusions and provider-limit pruning remain explicit `SkipReason` evidence in ResolutionPreview/GenerationRecipeSnapshot and are counted separately; they are not fake failed RunItems.

### Run projections

The Run maintains or rebuilds:

- `total_items` — executable RunItems only;
- exact counts for every RunItem state;
- `items_succeeded`, `items_reused`, `items_failed`, `items_cancelled`, `items_paused`, and `items_uncertain`;
- `items_active` — executable or in-flight states;
- `excluded_combinations`/`skip_reason_count` from the immutable snapshot;
- `requires_action` and summarized pause/uncertainty reasons;
- `revision`, `started_at`, and latest quiescent time.

Cost totals are not incremented as side effects of item-state transitions. `reserved_cost`, `settled_cost`, and remaining exposure come from the authoritative SpendReservation/cost-ledger projection, including Release and signed-direction Adjustment evidence.

Every item transition and Run projection update happens in one domain transaction, or the Run projection is rebuilt from RunItems. Tests must prove rebuilding produces the same result.

## RunItem state machine

### States

| State | Meaning |
|---|---|
| `queued` | Eligible work is waiting for the existing Job to be leased. |
| `preparing` | Worker owns the Job lease and is performing pre-dispatch validation/local preparation. No external-call seam has been crossed. |
| `awaiting_provider` | `dispatch_started` is durably committed. External outcome may now be ambiguous. |
| `processing_output` | Durable response evidence exists; local Asset/AssetVersion persistence is in progress or safely resumable. |
| `qc_pending` | At least one output version exists and required automated QC has not reached a final result. |
| `feedback_pending` | Blocking QC failed and the frozen bounded-feedback policy permits another generation attempt. |
| `retry_wait` | A definitely safe retry is scheduled after backoff. |
| `paused` | A recoverable gate prevents a new external step; no ambiguous external call is in flight. |
| `reused` | Terminal successful outcome using an existing eligible AssetVersion. No Provider call or real cost reservation occurred. |
| `succeeded` | Terminal successful outcome with a durably persisted AssetVersion and required blocking QC passed. Human review may still be pending. |
| `failed` | Quiescent failure for the current command/recovery cycle. Output/evidence may still exist. An explicit retry command may reopen only eligible failures. |
| `cancelled` | Quiescent cancellation before a required remaining step. Already persisted output/evidence is retained. |
| `uncertain` | A Provider step crossed `dispatch_started` but its outcome cannot yet be proved. Only reconciliation may leave this state. |

`reused`, `succeeded`, `failed`, and `cancelled` are quiescent outcomes. `failed` may be reopened only by a new explicit retry Operation. `succeeded`/`reused` are never retried; new Seed or prompt changes use regeneration.

### Primary execution flow

```text
queued
  └─ Job leased + worker validation → preparing
       ├─ cancel before dispatch → cancelled
       ├─ recoverable gate fails → paused
       ├─ safe local failure → retry_wait or failed
       └─ ProviderAttempt created + dispatch_started committed
            → awaiting_provider
                 ├─ definitive retryable failure → retry_wait
                 ├─ definitive terminal failure → failed
                 ├─ lease loss without durable response → uncertain
                 └─ response_received with durable evidence
                      → processing_output
                           └─ Asset/AssetVersion/binding committed
                                → qc_pending
                                     ├─ required QC passes → succeeded
                                     ├─ only nonblocking failures → succeeded
                                     ├─ blocking failure + budget remains
                                     │    → feedback_pending
                                     │         → preparing for a new ProviderAttempt
                                     └─ blocking failure + budget exhausted
                                          → failed(qc_exhausted)
```

Each successful generation response creates a new immutable AssetVersion before QC. A feedback attempt therefore produces a child AssetVersion rather than rewriting the failed version. Every prior version, attempt, prompt, QC result, and cost record remains queryable.

### Transition guards

- `queued|retry_wait → preparing` requires an owned, unexpired Job lease and CAS on the RunItem revision.
- Entering `preparing` does not imply a Provider call occurred.
- `preparing → awaiting_provider` requires, in one transaction:
- fresh Workspace/authorization/entitlement/credential-binding/right checks;
  - sufficient open cost exposure for the attempt where applicable;
  - an immutable ProviderAttempt header;
  - an append-only `dispatch_started` event;
  - Job state safe for paid-call ambiguity.
- `awaiting_provider → processing_output` requires durable `response_received` evidence. The response bytes/object reference and hash must be sufficient for local replay without another Provider call.
- `processing_output → qc_pending` requires an idempotently persisted Asset, AssetVersion, producing binding, content hash, and UsageEvent.
- `qc_pending → succeeded` requires all blocking QC checks to pass for the selected exact AssetVersion.
- `qc_pending → feedback_pending` requires a completed blocking failure, remaining feedback budget, and no cancellation intent.
- `feedback_pending → preparing` requires deterministic attempt input derived only from the recipe snapshot plus recorded QC feedback; it never resolves current Library versions.
- `awaiting_provider → uncertain` occurs on lease expiry when `dispatch_started` exists but no durable conclusive response exists.
- `uncertain` has no user-driven retry/resume transition.

### Pausing at external-step gates

Before every new external or paid step, revalidate:

- Workspace lifecycle and authorization epoch;
- current actor/system authority allowed for recovery;
- entitlement lease and concurrency capacity;
- credential-binding eligibility and, immediately before a credentialed call, availability of a current active CredentialVersion;
- rights, emergency Withdraw, and dependency eligibility;
- execution mode and provider capability;
- remaining SpendReservation exposure for paid `byok` steps, where applicable.

If a failure is recoverable without changing the frozen recipe, the item becomes `paused` with a stable reason and `same_snapshot_resumable=true`. Definitely unused reservation/capacity is released rather than held indefinitely.

Normal CredentialVersion rotation within the frozen binding does not change the recipe and is same-snapshot resumable. If recovery would require changing the frozen credential-binding identity, provider/capability decision, withdrawn dependency, prompt, reference, or cost contract, record `new_preview_required`; `resume` rejects and the user must create a new Draft/Preview/Run intent. No Worker silently substitutes a different frozen semantic input.

## Job and ProviderAttempt separation

Job attempts and Provider attempts are different counters:

- `Job.attempt` counts scheduler lease/worker attempts.
- `ProviderAttempt.attempt_number` counts actual planned external calls.
- `feedback_round` counts bounded QC-driven regeneration rounds.
- Recipe/quote evidence freezes independent limits and a maximum total paid-call exposure.

A pre-dispatch Worker crash may increment Job attempts without consuming a Provider attempt or provider-call budget. A new ProviderAttempt is created only when a new external call is genuinely planned.

Automatic retry reuses the existing Job row through `retry_wait` and `next_attempt_at`. It does not enqueue a new Job. A manual retry after a quiescent `failed` outcome creates a new command Operation and a new Job/recovery cycle for the same RunItem while preserving every earlier Job and ProviderAttempt record.

No retry/feedback combination may exceed:

- the frozen transport/provider retry limit;
- the frozen QC-feedback limit;
- the frozen maximum total paid calls;
- the currently confirmed and reserved maximum exposure.

If the product allows separate retry and feedback budgets, ResolutionPreview must quote their worst-case composition. An unquoted attempt is rejected.

## ProviderAttempt lifecycle

ProviderAttempt does not use one linear enum ending in `settled`. Its projection has orthogonal dimensions:

### Dispatch state

```text
planned → dispatch_started → response_recorded
```

### Outcome

```text
pending | succeeded | failed | no_result
```

### Certainty/reconciliation

```text
certain | uncertain | reconciled
```

### Settlement state

```text
not_applicable | exposure_open | reported | estimated | held_uncertain | adjusted | finalized
```

Rules:

- The immutable ProviderAttempt header represents `planned`.
- `dispatch_started` is persisted before the adapter call.
- A Worker loss after `dispatch_started` without conclusive response evidence makes certainty `uncertain` and settlement `held_uncertain`.
- `response_received`, usage, and reconciliation are separate append-only facts.
- Provider success does not imply AssetVersion persistence or QC success.
- Cost settlement does not rewrite external-call outcome.
- Event ordering uses monotonic sequence numbers, and appending an event plus advancing the projection is atomic.
- Rebuilding from events must reproduce all four projection dimensions.

## AssetQCAttempt lifecycle

`AssetQCAttempt` is an immutable identity/input plus CAS state projection. Individual `AssetQCCheckResult` rows and audit evidence are append-only.

```text
created → running
             ├─ passed
             ├─ failed_nonblocking
             ├─ failed_blocking
             ├─ error
             └─ uncertain
```

Rules:

- Every QCAttempt is scoped to one exact AssetVersion and frozen QC policy/rule versions.
- External visual QC uses a ProviderAttempt with `step_kind=visual_qc`; its dispatch ambiguity and cost follow the same ProviderAttempt rules.
- A completed `failed_blocking` permanently prevents approval eligibility for that exact AssetVersion.
- Retrying a QC infrastructure error creates a new QCAttempt; it does not delete the error attempt.
- QC feedback generation creates a new generation ProviderAttempt and a new child AssetVersion. A later version passing QC does not rewrite the failed version.
- QC never appends a human `Approved` QualityReview.
- When feedback budget is exhausted, RunItem becomes `failed` with `qc_exhausted`; all AssetVersions remain. Effective eligibility may project Rejected/blocked without fabricating a human review record.

## Reuse flow

Reuse is decided inside atomic submission, before paid Reservations and Jobs:

1. Compute the frozen semantic RunItem fingerprint, including Workspace, recipe inputs, actual reference choices, execution mode, provider/model semantics, Seed intent, format, exclusions, and relevant rights/QC policy.
2. Lock/claim the fingerprint so concurrent submissions cannot both dispatch.
3. Find an existing successful AssetVersion whose producer evidence, object existence/hash, mode, rights, and blocking-QC eligibility remain valid.
4. Create a new RunItem directly in `reused` and append a `run_item_asset_version_binding(kind='reused')`.
5. Create reuse Usage/audit evidence.
6. Do not create a Job, ProviderAttempt, real SpendReservation, or duplicate AssetVersion.

The source AssetVersion and original producing RunItem remain unchanged. Reuse is visible separately in Run counts and provenance even though it contributes to successful Run outcome.

## Crash recovery matrix

`recover_expired_jobs()` evaluates durable domain evidence, not only the generic Job state:

| Durable evidence | Recovery action |
|---|---|
| No `dispatch_started` | Safely requeue the existing Job/local work. Reuse the planned ProviderAttempt if its immutable request remains valid; do not increment provider-call count. |
| `dispatch_started`, no conclusive response | RunItem and ProviderAttempt become `uncertain`; retain exposure and begin reconciliation. |
| Durable response, no AssetVersion | Requeue local `processing_output` using stored response evidence. Never call Provider again. |
| AssetVersion/binding exists, QC absent/incomplete | Resume `qc_pending` idempotently. |
| AssetVersion and terminal QC exist | Rebuild RunItem/Run/Operation projections from durable facts. |
| External QC `dispatch_started`, no result | RunItem becomes `uncertain` with uncertainty reason `visual_qc`; reconcile that ProviderAttempt. |

Recovery first applies the existing authorization-epoch gate. A stale or disabled Workspace cannot start a new external step. It may only persist/close evidence from an already completed atomic call, settle or reconcile cost, and append audit history.

## Reconciliation flow

For an uncertain ProviderAttempt:

1. Use provider-supported lookup by durable request/idempotency identity when available.
2. Append reconciliation request/response evidence.
3. Resolve according to evidence:
   - output/result found → append response evidence, move RunItem to `processing_output`, persist output, then perform QC;
   - request definitely never accepted and no charge → certainty resolves; move to `retry_wait` if the same confirmed retry budget remains, otherwise `failed` or `paused` pending fresh intent;
   - definitive Provider failure → settle any evidenced charge and classify retry eligibility;
   - still unknowable → remain `uncertain`, retain exposure, and require human/provider escalation.
4. Never transition directly from `uncertain` to `succeeded` merely because a lookup says "success"; output persistence and QC gates still apply.

User retry/resume commands are rejected while certainty remains unresolved.

## Cancellation flow

`cancel_run` is an idempotent command with expected Run revision. It creates a cancellation Operation and records Run/item `cancel_requested_at` intent.

Per-item behavior:

- `queued`, `retry_wait`, `paused`, or `preparing` with no `dispatch_started`: cancel Job/local work, transition to `cancelled`, release definitely unused Reservation/capacity.
- `awaiting_provider` or any uncertain ProviderAttempt: do not pretend the call stopped and do not release its exposure. Persist/settle/reconcile the current attempt.
- `processing_output`: finish idempotent local persistence so paid output/evidence is not discarded.
- `qc_pending`: finish already-running deterministic/local QC; do not begin a new paid QC or feedback step after cancellation.
- `feedback_pending`: transition to `cancelled` and retain every prior AssetVersion/QC result.
- `succeeded`, `reused`, `failed`, or already `cancelled`: unchanged.

After an in-flight call closes:

- if output and required QC already pass, preserve `succeeded` with completion reason `completed_after_cancel_request`;
- if output exists but required QC cannot/should not begin, preserve the AssetVersion and terminate `cancelled` with explicit incomplete-QC evidence;
- if the call definitely failed, settle/release correctly and terminate `cancelled` or `failed` according to the recorded evidence;
- if outcome is ambiguous, remain `uncertain`.

Settled costs remain. Only definitely unused exposure is released. Cancellation never erases output or historical evidence.

Item-level cancel is not exposed in v1; the Run command applies item-specific rules internally.

## Automatic retry flow

Automatic retry is allowed only after a definitive, policy-classified safe failure such as a provider-declared transient error or rate limit that does not leave call outcome ambiguous.

1. Append failure/certainty/cost evidence to the current ProviderAttempt.
2. Check provider-call limit, QC-feedback limit where applicable, total paid-call cap, remaining confirmed exposure, and cancellation intent.
3. Move RunItem to `retry_wait` and the existing Job to `retry_wait` with bounded backoff.
4. On the next lease, move RunItem to `preparing`.
5. Create a new ProviderAttempt only immediately before a genuinely new external call.
6. If any limit is exhausted, transition to `failed` and release only exposure no longer needed for certain attempts.

A timeout or Worker loss after `dispatch_started` is not an automatic-retry signal. It is `uncertain` until reconciled.

## Explicit retry, resume, and regeneration

### `retry_failed_items`

- Accepts a Run and explicit failed item IDs, or an explicit "all eligible failed" selector resolved under lock.
- Rejects succeeded, reused, cancelled, paused, and uncertain items.
- Uses expected revision and idempotency key.
- Creates a new recovery Operation and recovery-cycle evidence while preserving all prior attempts/jobs/errors.
- Revalidates Workspace, entitlement, rights, credential binding/mode, capability, and cost.
- Requires a fresh `run_recovery` ResolutionPreview and confirmation for the exact selection. The Preview records a zero additional quote when remaining confirmed exposure is sufficient, or the bounded additional exposure otherwise.
- Reopens eligible items as `queued` and creates a new Job for the new manual recovery cycle.
- Does not change Seed, prompt, references, provider mode, or recipe content.

### `resume_paused_items`

- Targets only paused items whose reason is marked `same_snapshot_resumable`.
- Requires a fresh `run_recovery` ResolutionPreview and confirmation for the exact paused-item selection, revalidates every external-step gate, and reacquires released capacity/cost exposure when required.
- Creates a new recovery Operation/Job as needed.
- Rejects if capability, credential-binding identity, frozen dependency, price contract, or rights would need substitution; normal CredentialVersion rotation inside the same binding remains eligible for revalidation and resume.

### `reconcile_uncertain_attempt`

- System/reconciler command, or tightly authorized human evidence command.
- Never accepts "retry anyway" as evidence.
- Appends lookup/reconciliation evidence and follows the reconciliation flow above.

### Regeneration commands

Same-recipe reproduction, new Seed, edited Prompt, and quality-feedback regeneration are not retries. They create a new explicit Draft/Preview/Confirmation/Snapshot/Run intent as applicable and a new AssetVersion linked to its source version. Historical reviews, QC, attempts, and versions remain unchanged.

## Cost exposure lifecycle

Each paid RunItem/recovery intent has conservative maximum exposure frozen by the confirmed quote. The exposure covers every permitted provider retry and QC-feedback call under the frozen limits.

Conceptual SpendReservation projection:

```text
open
  ├─ certain terminal outcome → settled_and_released
  ├─ definitely no dispatch/cancel/pause → released
  └─ ambiguous dispatch → held_uncertain
         ├─ reconciled → settled_and_released
         ├─ proved no charge + retry allowed → open
         └─ unresolved → held_uncertain
```

Rules:

- Reservation is atomic with admission before Job creation for paid non-reused work.
- Immediately before every paid call, remaining exposure must cover that attempt's frozen maximum.
- Reported Cost or Estimated Settlement references the exact ProviderAttempt.
- Settlement consumes evidenced cost; Release frees the unused remainder only when no unresolved attempt needs it.
- Adjustment is append-only, directional, and references the corrected entry and reconciliation evidence.
- Run cost projections include Releases and Adjustments; they never naïvely sum Reported plus Estimated records.
- Cancellation, failure, or pause before dispatch releases definitely unused exposure.
- Uncertain attempts retain exposure until reconciliation; no sibling request may spend it.
- `sandbox` and `mock` use the same Job/domain/recovery path but create no payable SpendReservation or provider-cost ledger entries. Mock simulated estimates remain mode-isolated scenario evidence.

## Partial success and downstream eligibility

A Run may become `partially_succeeded` only after all non-uncertain/non-paused work is quiescent and at least one item succeeded/reused while another failed/cancelled.

Per-item detail exposes:

- AssetVersion bindings and whether they were produced or reused;
- ProviderAttempt and Job history;
- QC attempts/results and blocking eligibility;
- error, pause, cancellation, and uncertainty reasons;
- cost evidence and reconciliation state.

Succeeded/reused AssetVersions remain available for authorized review and applicable exports regardless of sibling failure. They enter Experiments only after exact-version human approval and every other experiment eligibility gate. A Run sibling's failure cannot rewrite or hide a successful version.

## State-machine interfaces

The implementation should concentrate transition logic behind a small set of deep module interfaces, conceptually:

```text
claim_or_prepare_run_item(...)
begin_provider_attempt(...)
record_provider_response(...)
persist_provider_output(...)
record_qc_outcome(...)
schedule_safe_retry(...)
mark_attempt_uncertain(...)
reconcile_provider_attempt(...)
cancel_generation_run(...)
retry_failed_items(...)
resume_paused_items(...)
```

Each interface validates the complete transition, appends immutable evidence, advances all affected projections, and returns the resulting domain state. HTTP handlers and Workers do not issue ad hoc state updates.

## Required state-machine evidence

Section 2 is complete only when tests cover:

- Run projection precedence for every mixture of active, paused, uncertain, reused, succeeded, failed, and cancelled items;
- Operation immutability across later recovery Operations;
- Job-attempt versus ProviderAttempt counter separation;
- crash points before dispatch, after dispatch, after response, after output persistence, and during QC;
- concurrent reuse/fingerprint claims before Reservation/Job creation;
- retry/feedback caps and maximum quoted exposure;
- cancellation races at every external-call seam;
- uncertain Reservation retention and reconciliation;
- blocking QC permanence on the exact AssetVersion;
- manual retry eligibility and preservation of attempt history;
- resume requiring unchanged frozen inputs;
- partial-success usability and exact-version downstream gates;
- cross-Workspace and authorization-revocation failures at every command.

---

# Section 3: HTTP Command Surface, Response Projections, and Worker Handlers

This section defines the private FastAPI/OpenAPI interface used by the BFF, the bounded projections returned to callers, and the Worker handlers that cross the module interfaces established in Sections 1 and 2.

## Routing and identity

All routes below are private FastAPI routes under:

```text
/api/v1/*
```

They never contain a Workspace slug or Workspace ID. FastAPI constructs `TenantContext` exclusively from the verified delegated JWT. A Workspace/actor/role supplied in a path, query, or request body is never accepted as authority.

The BFF exposes the separate browser-facing route shape:

```text
/api/domain/v1/w/{workspace_slug}/*
```

The BFF resolves the slug, verifies active membership, signs the short-lived delegated JWT, calls the fixed private FastAPI path, and mechanically maps private `Location` headers back to the same-origin BFF route. It does not reinterpret domain responses.

## Common HTTP semantics

### Headers

- Every mutation carries `Idempotency-Key`.
- Commands that read or change a mutable aggregate carry `If-Match` using the ETag returned by its latest projection.
- GET detail responses return `ETag` and honor `If-None-Match` with `304`.
- Asynchronous commands return `202 Accepted` with `Location: /api/v1/operations/{operation_id}`.
- Created synchronous resources return `201 Created` with their resource `Location`.
- Nonterminal Operation responses retain the existing `Retry-After` behavior.

`If-Match` is the HTTP representation of the module's `expected_revision`; request bodies do not carry a second competing revision field.

### Idempotency protocol

The HTTP adapter computes a normalized fingerprint from method, private path, normalized query, typed body, Workspace, actor, and command type.

The implementation uses the existing durable pending-reservation protocol:

1. Claim or find the idempotency reservation before running the domain body.
2. Same key/binding/fingerprint returns or waits for the existing result.
3. Same key with a different binding/fingerprint returns `409 idempotency_key_reused`.
4. The winner locks the pending record inside the domain transaction, creates the result, and finalizes the reservation with the resource/Operation identity.
5. A winner crash leaves one recoverable pending reservation, never authority for a second command body.

HTTP idempotency is not the Worker's retry mechanism. Workers continue to use durable Jobs, leases, semantic fingerprints, ProviderAttempt identities, and cost reservations.

### Representation rules

- Request/response models use explicit typed fields and reject unknown fields.
- Decimal/money values serialize as decimal strings, never JSON floats.
- Timestamps are timezone-aware ISO 8601 values.
- Enums are explicit OpenAPI enums; no ellipsis/open-ended string maps stand in for required contract fields.
- Lists are bounded and paginated with opaque stable cursors.
- Provider request/response bodies, credential plaintext, signed URLs, arbitrary object keys, and sensitive full prompts never appear in generic Run/Timeline responses.
- Authorized prompt views use a dedicated redacted prompt-evidence projection.

### Errors

Every non-success response uses `application/problem+json` with stable `code`, `trace_id`, `retryable`, and `field_errors`.

The interface distinguishes at least:

```text
idempotency_key_required
idempotency_key_reused
revision_conflict
draft_archived
preview_expired
preview_stale
recovery_preview_stale
dependency_withdrawn
rights_ineligible
credential_ineligible
mode_unavailable
provider_capability_changed
combination_limit_exceeded
coverage_budget_insufficient
quote_changed
authorization_epoch_stale
entitlement_sync_pending
spend_limit_exceeded
run_state_conflict
item_selection_invalid
new_preview_required
approval_blocked_by_qc
provider_outcome_uncertain
export_source_ineligible
mixed_execution_modes
```

The client branches only on status/code/retryable/field errors, never localized `detail` text.

## Draft routes

| Method | Private path | Headers | Response |
|---|---|---|---|
| `GET` | `/api/v1/generation/drafts` | — | `200` paginated Draft summaries |
| `POST` | `/api/v1/generation/drafts` | `Idempotency-Key` | `201` Draft projection + `Location` |
| `GET` | `/api/v1/generation/drafts/{draft_id}` | `If-None-Match` optional | `200` Draft projection + `ETag` |
| `PATCH` | `/api/v1/generation/drafts/{draft_id}` | `Idempotency-Key`, `If-Match` | `200` updated Draft projection + new `ETag` |
| `POST` | `/api/v1/generation/drafts/{draft_id}/archive` | `Idempotency-Key`, `If-Match` | `200` archived Draft projection |

There is no generic `DELETE`. Archive is an explicit command, preserves referenced history, and prevents later preview creation unless a dedicated restore command is added in a future contract.

Draft list supports bounded filters such as `state`, `product_id`, `regeneration_intent`, `created_after`, and opaque cursor pagination with stable `(created_at, id)` ordering.

### Draft projection

```text
id
workspace_id
state: active | archived
selectors:
  product
  offer
  dimension_schema
  people
  references
  prompt_policy
  data_sources
  provider
  model
  format
  execution_mode
combination_strategy
exclusions
regeneration_intent
source_asset_version_id
source_recipe_snapshot_id
revision
created_at
updated_at
```

Every selector explicitly identifies `latest_published_at_preview` or `pinned` plus its version ID. The Draft never claims those selectors are resolved execution versions.

## Initial ResolutionPreview routes

| Method | Private path | Headers | Response |
|---|---|---|---|
| `POST` | `/api/v1/generation/drafts/{draft_id}/previews` | `Idempotency-Key`, `If-Match` | `201` Preview projection + `Location` |
| `GET` | `/api/v1/generation/previews/{preview_id}` | `If-None-Match` optional | `200` Preview projection + `ETag` |
| `GET` | `/api/v1/generation/previews/{preview_id}/items` | — | `200` paginated immutable Preview entries |

Preview creation body is empty unless a future typed preview option is added. Draft selectors and the `If-Match` precondition are the complete input.

Preview creation resolves all selectors, actual references, versioned policies, provider capabilities, credential-binding eligibility, execution mode, complete combination plan, exclusions/SkipReasons, final prompt evidence, and maximum quote. Invalid drafts return typed Problem Details and do not persist a misleading "valid" Preview.

### Preview projection

```text
id
purpose: initial_generation
preview_hash
draft_id
draft_revision
status: fresh | expired | stale
resolved_version_summary
combination_summary:
  candidate_combinations
  feasible_combinations
  selected_combinations
  executable_items
  skipped_combinations
  planner_version
  constraint_set_id
  constraint_set_version
  strategy
  coverage_strength
  coverage_obligations_total
  coverage_obligations_covered
  selected_fingerprints_hash
  skip_reason_partitions_hash
provider_capability_summary
execution_mode
quote:
  provider_amount
  provider_currency
  budget_amount
  budget_currency
  exchange_rate_evidence
  price_rule_version
  valid_until
warnings
expires_at
created_at
```

Preview item entries expose every planned executable item and every excluded/pruned choice using a stable disposition:

```text
ordinal
disposition: executable | skipped
stable_combination_identity
dimension_values
variant_index
requested_seed
actual_reference_asset_version_ids
provider_limit_decisions
skip_reasons
final_prompt_evidence
item_quote
```

Pagination never changes what the Preview hash covers. The hash covers the full canonical server-side entry set, not only the currently requested page. There is no silent truncation.

## Recovery ResolutionPreview routes

Every manual retry/resume first creates and confirms an immutable recovery Preview. When no additional exposure is needed, the Preview freezes a zero additional quote rather than bypassing the recovery path:

| Method | Private path | Headers | Response |
|---|---|---|---|
| `POST` | `/api/v1/generation/runs/{run_id}/recovery-previews` | `Idempotency-Key`, `If-Match` | `201` recovery Preview + `Location` |
| `GET` | `/api/v1/generation/previews/{preview_id}` | `If-None-Match` optional | Same Preview resource/interface |

Request:

```json
{
  "command": "retry_failed_items",
  "selection": {
    "kind": "item_ids",
    "item_ids": ["ri_..."]
  }
}
```

Allowed command/selection combinations:

- `retry_failed_items` with explicit `item_ids` or `all_eligible_failed`;
- `resume_paused_items` with explicit `item_ids` or `all_resumable_paused`.

The Preview freezes exact selected item IDs, current states/revisions, remaining original exposure, required additional exposure, current gates, and whether the same recipe snapshot remains resumable. It never changes recipe content or substitutes current Library versions.

## Initial submission

| Method | Private path | Headers | Response |
|---|---|---|---|
| `POST` | `/api/v1/generation/runs` | `Idempotency-Key` | `202` Operation acceptance + `Location` |

Request:

```json
{
  "preview_id": "rp_...",
  "preview_hash": "..."
}
```

Response:

```json
{
  "operation_id": "op_...",
  "state": "queued",
  "generation_run_id": "gr_...",
  "result": {
    "resource_type": "generation_run",
    "resource_id": "gr_..."
  }
}
```

The response remains `202` even if every item short-circuits to `reused`; the returned Operation may already project `succeeded`.

### Admission sequence

After winning the durable idempotency reservation, the domain transaction:

1. locks the pending idempotency record and Preview;
2. validates Workspace lifecycle, actor role, authorization epoch, entitlement lease, and capacity;
3. validates Preview purpose/freshness/hash, Draft revision, dependency status, rights, credential-binding eligibility, provider capabilities, quote, and Spend Guardrail;
4. resolves reuse under fingerprint locks before creating paid Reservations or Jobs;
5. creates the Confirmation, immutable RecipeSnapshot, Operation, GenerationRun, initial GenerationRunCommand, and RunItems;
6. creates Quote/Reservation/capacity/Job evidence only for non-reused work;
7. appends dependency, Usage, and audit evidence;
8. finalizes the idempotency outcome and commits without crossing a Provider seam.

The unique initial Preview confirmation prevents two different HTTP keys from creating two Runs from one confirmed Preview. A second key returns `preview_already_submitted` with the existing result reference or an idempotent equivalent response; it never creates a duplicate paid intent.

## Run query routes

| Method | Private path | Response |
|---|---|---|
| `GET` | `/api/v1/generation/runs` | Paginated Run summaries |
| `GET` | `/api/v1/generation/runs/{run_id}` | Full bounded Run projection + `ETag` |
| `GET` | `/api/v1/generation/runs/{run_id}/items` | Paginated RunItem summaries |
| `GET` | `/api/v1/generation/runs/{run_id}/items/{item_id}` | Full bounded RunItem projection + `ETag` |
| `GET` | `/api/v1/generation/runs/{run_id}/items/{item_id}/provider-attempts` | Paginated attempt summaries |
| `GET` | `/api/v1/generation/runs/{run_id}/timeline` | Paginated redacted event timeline |
| `GET` | `/api/v1/generation/runs/{run_id}/cost` | Cost projection by mode/currency/item/evidence type |

Run list supports bounded filters including `state`, `product_id`, `execution_mode`, `created_after`, and opaque cursor pagination.

### Run projection

```text
id
workspace_id
creation_operation_id
creation_preview_confirmation_id
recipe_snapshot_id
submission_kind
state
cancel_requested_at
requires_action
action_reasons
total_items
exact item-state counts
items_succeeded
items_reused
items_failed
items_cancelled
items_uncertain
items_paused
excluded_combinations
skip_reason_count
cost_summary:
  budget_currency
  real_reserved
  real_settled
  real_released
  real_uncertain_held
  simulated_estimate
revision
created_at
started_at
updated_at
```

### RunItem projection

```text
id
state
ordinal
stable_combination_identity
dimension_values
variant_index
seed:
  requested
  effective
  availability
  unavailable_reason
semantic_fingerprint
intent_kind
source_asset_version_id
selected_result:
  asset_version_id
  relationship: produced | reused
provider_attempt_summary:
  total
  uncertain
  latest_outcome
qc_summary:
  attempts
  effective_blocking_failures
  feedback_round
pause_or_error:
  code
  retryable
  same_snapshot_resumable
cancel_requested_at
revision
created_at
updated_at
```

Attempts, QC results, reviews, and cost entries are not embedded without bound. Detail projections contain summaries and linkable IDs; dedicated paginated routes expose history.

### Timeline projection

The timeline is a redacted projection of GenerationRunCommand, Job/audit events, ProviderAttempt events, QC outcomes, cost evidence, and cancellation/reconciliation events.

Each entry contains:

```text
event_id
sequence
occurred_at
event_schema_version
event_type
run_item_id
provider_attempt_id
actor_or_executor_kind
redacted_summary
```

Cursor order is `(sequence, event_id)`, not timestamp alone. Raw Provider payloads, object keys, full prompts, signed URLs, credential identifiers inappropriate for Members, and secret/error stack data are excluded.

### Cost projection

Money is returned as decimal strings. Provider and Workspace-budget currencies remain distinct. BYOK/Sandbox/Mock partitions are not merged.

```text
budget_currency
byok:
  total_reserved
  total_reported
  total_estimated
  total_released
  total_adjustments_increase
  total_adjustments_decrease
  net_settled
  net_exposure
  uncertain_held
sandbox:
  payable: "0"
mock:
  payable: "0"
simulated_mock:
  provider_estimate
  budget_estimate
by_item: paginated or separately cursor-addressable
by_evidence_type
as_of
```

The projection follows Reservation/Release/Adjustment references; it does not naïvely sum Reported and Estimated entries.

## Run recovery commands

| Method | Private path | Headers | Response |
|---|---|---|---|
| `POST` | `/api/v1/generation/runs/{run_id}/cancel` | `Idempotency-Key`, `If-Match` | `202` cancellation Operation + `Location` |
| `POST` | `/api/v1/generation/runs/{run_id}/retry-failed-items` | `Idempotency-Key`, `If-Match` | `202` retry Operation + `Location` |
| `POST` | `/api/v1/generation/runs/{run_id}/resume-paused-items` | `Idempotency-Key`, `If-Match` | `202` resume Operation + `Location` |

Cancel request:

```json
{
  "reason": "User requested cancellation"
}
```

Cancel atomically records intent, creates GenerationRunCommand/Operation evidence, immediately cancels definitely pre-dispatch items where safe, and returns `202`. In-flight work drains, settles, or reconciles under Section 2; the client polls the cancellation Operation.

Retry/resume request:

```json
{
  "preview_id": "rp_...",
  "preview_hash": "..."
}
```

Selection comes exclusively from the immutable recovery Preview. The command revalidates Run revision, exact target states, eligibility, quote, and exposure before creating its Confirmation, GenerationRunCommand, Operation, Reservations, and Jobs.

Only invalid targeted items reject the command. An uncertain sibling outside the frozen selection does not by itself block retrying an eligible failed item; its held exposure still participates in the atomic Spend Guardrail check.

## Asset and review routes

| Method | Private path | Response |
|---|---|---|
| `GET` | `/api/v1/assets` | Paginated Asset summaries |
| `GET` | `/api/v1/assets/{asset_id}` | Asset projection + `ETag` |
| `GET` | `/api/v1/assets/{asset_id}/versions` | Paginated exact-version history |
| `GET` | `/api/v1/assets/{asset_id}/versions/{asset_version_id}` | Version projection + `ETag` |
| `GET` | `/api/v1/assets/{asset_id}/versions/{asset_version_id}/qc-attempts` | Paginated QC attempts/results |
| `GET` | `/api/v1/assets/{asset_id}/versions/{asset_version_id}/reviews` | Paginated human-review history |
| `POST` | `/api/v1/assets/{asset_id}/versions/{asset_version_id}/reviews` | `201` appended review |

The review command requires `Idempotency-Key` and `If-Match` for the current version eligibility/review projection. It validates exact Workspace/version nesting, actor role, Run/QC state, and permanent blocking failures.

Review body:

```json
{
  "verdict": "approved",
  "reason": "..."
}
```

Allowed verdicts are `approved`, `needs_changes`, and `rejected`. `approved` with any effective blocking QC failure returns `409 approval_blocked_by_qc`; it never creates a review row.

### AssetVersion projection

```text
id
asset_id
origin_kind
parent_version_id
producing_run_item_id
provider_attempt_id
recipe_snapshot_id
seed evidence
content_sha256
byte_size
mime_type
generation_provenance_summary
qc_summary:
  attempts
  passed
  permanent_blocking_failures
  nonblocking_failures
human_review_summary:
  current_verdict
  latest_review_id
  review_count
approval_eligibility:
  eligible
  reasons
ready_for_experiment:
  eligible
  noncontextual_reasons
  evaluated_at
eligibility_revision
created_at
```

`ready_for_experiment` is only the non-contextual Asset gate. Section #22 owns Experiment-context eligibility and verdicts; this projection never claims a global `experiment_eligible=true` or manufactures Winner/Loser truth.

Asset list filters use explicit version-producing Run semantics. A stable Asset's origin Run is not silently treated as the producing Run for all later versions.

## Regeneration draft command

| Method | Private path | Headers | Response |
|---|---|---|---|
| `POST` | `/api/v1/generation/drafts/from-asset-version` | `Idempotency-Key` | `201` Draft projection + `Location` |

Request:

```json
{
  "source_asset_version_id": "av_...",
  "intent": "quality_feedback",
  "qc_attempt_id": "qca_..."
}
```

Intent enum:

```text
same_recipe
new_seed
edit_prompt
quality_feedback
```

`qc_attempt_id` is required only for `quality_feedback` and must identify immutable QC evidence for the exact source version. The command creates a Draft with pinned/provenance selectors appropriate to the intent. It never calls a Provider or bypasses Preview/Confirmation/submission.

## Export interface

### Create export

| Method | Private path | Headers | Response |
|---|---|---|---|
| `POST` | `/api/v1/exports` | `Idempotency-Key` | `202` Operation + ExportPackage identity + `Location` |

Run selector:

```json
{
  "source": {
    "kind": "generation_run",
    "generation_run_id": "gr_...",
    "scope": "approved",
    "experiment_context_id": null,
    "include_failed_evidence": false
  }
}
```

Exact-version selector:

```json
{
  "source": {
    "kind": "asset_versions",
    "asset_version_ids": ["av_...", "av_..."]
  }
}
```

Run scopes:

- `all_successful` — selected produced/reused versions whose RunItem succeeded;
- `approved` — exact versions with current human Approved and no blocking QC;
- `winner` — requires an explicit Experiment/context identity and #22 contextual Winner evidence.

At command time, the module resolves and freezes the exact AssetVersion IDs, business references, review/QC/verdict context, selector evidence, and one generation execution mode into ExportPackage. Later changes do not silently alter package membership. An exact-version selection spanning `byok`, `sandbox`, or `mock` is rejected with `409 mixed_execution_modes`; callers create separate packages per mode.

`include_failed_evidence` adds failed/uncertain item evidence to Manifest; it does not pretend failed items have binary AssetVersion files.

Response:

```json
{
  "operation_id": "op_...",
  "state": "queued",
  "export_package_id": "xp_...",
  "result": {
    "resource_type": "export_package",
    "resource_id": "xp_..."
  }
}
```

### ExportPackage and download

| Method | Private path | Headers | Response |
|---|---|---|---|
| `GET` | `/api/v1/export-packages/{export_package_id}` | `If-None-Match` optional | Package metadata + `ETag` |
| `POST` | `/api/v1/export-packages/{export_package_id}/download-intent` | `Idempotency-Key` | `201` fresh DownloadIntent/grant |

ExportPackage projection exposes state, selection counts, Manifest/ZIP hashes, byte size, retention, and failure reason. It never returns its internal object key or a signed URL.

Each ExportPackage has exactly one frozen generation execution mode. Manifest entries repeat it explicitly; it is never inferred from filenames or provider names.

DownloadIntent creation revalidates current Workspace membership/authorization, resolves the internal committed blob reference server-side, and issues a fresh short-lived capability through the existing DownloadIntent/BlobStore module. Export completion never creates a capability that may expire before the user polls.

### ZIP/Manifest requirements

- Exact requested/frozen versions must all validate; missing, forbidden, or hash-mismatched required content fails closed rather than being silently omitted.
- File paths are server-generated, normalized, length-bounded, and traversal-safe.
- File order and Manifest serialization are canonical/deterministic.
- Identical content hashes produce one binary file while Manifest retains every business reference.
- Manifest includes exact AssetVersion, Run/RunItem, recipe snapshot, ProviderAttempt, provider/model/API version, execution mode, Seed availability, references, prompt evidence, QC, human review, contextual verdict where requested, dual status, cost evidence, and hashes.
- Manifest excludes secrets, credential plaintext, signed URLs, internal arbitrary object keys, and cross-Workspace identifiers.
- Package row stores both Manifest hash and ZIP hash.

## Operation polling

The existing route remains authoritative:

```text
GET /api/v1/operations/{operation_id}
```

It retains ETag/`If-None-Match`, `Retry-After` for nonterminal states, stable Operation enums, and result references. No Workspace appears in the private path.

## Job dispatch contract

New work uses new Job types rather than overloading the legacy `generate_asset` type:

| Job type | Payload version | Payload IDs |
|---|---:|---|
| `generation.run_item` | `1` | `run_item_id`, `generation_run_command_id`, `recovery_cycle` |
| `generation.qc_asset_version` | `1` | `run_item_id`, `asset_version_id`, `feedback_round` |
| `generation.reconcile_provider_attempt` | `1` | `provider_attempt_id` |
| `generation.export_package` | `1` | `export_package_id`, `operation_id` |

The dispatch table is keyed by exact `(job.type, payload_version)`. Unknown versions fail before side effects with stable evidence. Payloads contain identifiers only—never recipe JSON, full prompt, Provider request/response, signed URL, credential material, or user-supplied Workspace identity.

Job initiator/role/request/trace/authorization epoch remain the frozen attribution used to construct the Worker system TenantContext. Every handler enters tenant scope/RLS before reading domain rows.

## `generation.run_item` Worker handler

The same handler processes initial, safe-retry, recovery-cycle, and QC-feedback generation. Attempt kind/input is stored in domain state; there is no duplicated feedback handler.

1. Load Job/RunItem/Run/RecipeSnapshot through the Job's TenantContext.
2. Call `claim_or_prepare_run_item()`:
   - verify lease and CAS revision;
   - return idempotent no-op for already completed work;
   - apply cancellation or pause gates before dispatch;
   - verify a selected/reused result was not already committed.
3. Revalidate every external-step gate and sufficient remaining exposure.
4. For `byok` and credentialed `sandbox`, resolve the frozen binding's current active CredentialVersion from the credential/KMS boundary only now and pass its ID into ProviderAttempt creation; for `mock`, resolve no credential. Secret plaintext remains process-memory scoped and is never persisted/logged.
5. Deterministically build ProviderCallRequest from RecipeSnapshot, RunItem, and recorded feedback evidence. Record a request hash/redacted evidence, not plaintext secrets.
6. Call `begin_provider_attempt()` in one transaction:
   - create/reuse the immutable planned ProviderAttempt;
   - append `dispatch_started`;
   - advance ProviderAttempt projection;
   - mark Job running and RunItem `awaiting_provider`;
   - rebuild Run projection;
   - commit before calling the adapter.
7. Execute the Provider adapter outside the database transaction.
8. Classify the adapter outcome:
   - definitely not dispatched → append typed pre-dispatch failure and schedule safe retry/failure;
   - conclusive Provider failure/response → durably store redacted response evidence, append events, settle that attempt, then classify retry/failure;
   - ambiguity after the seam → mark ProviderAttempt/RunItem/Job uncertain and retain exposure;
   - success → durably store response bytes/evidence, then append `response_received`.
9. Call `persist_provider_output()` idempotently:
   - create or reuse stable Asset identity;
   - create immutable AssetVersion and parent lineage;
   - append produced binding, content hash, and UsageEvent;
   - transition RunItem through `processing_output` to `qc_pending`;
   - rebuild Run projection.
10. Settle the generation ProviderAttempt from Reported Cost or Estimated Settlement evidence. Do not release the RunItem's remaining exposure while QC/feedback may still need it.
11. Enqueue idempotent `generation.qc_asset_version` for the exact version/round.
12. Mark the generation Job succeeded. The RunItem remains `qc_pending` until the QC Job closes it.

Only an adapter result that proves no dispatch may take the safe-retry path. Exceptions/timeouts after `dispatch_started` default to uncertain.

## `generation.qc_asset_version` Worker handler

1. Load the exact RunItem, AssetVersion, RecipeSnapshot QC policy, and existing QCAttempt under TenantContext.
2. Create/resume the QCAttempt identity and CAS projection idempotently.
3. Run deterministic checks and persist append-only individual results.
4. If external visual QC is required, create a ProviderAttempt with `step_kind=visual_qc` and use the same dispatch/certainty/cost coordinator as generation.
5. Finalize QCAttempt:
   - blocking checks pass / only nonblocking failures → RunItem `succeeded`;
   - blocking failure with frozen feedback budget/exposure → RunItem `feedback_pending`, persist deterministic feedback input, enqueue a new `generation.run_item` Job for the next round;
   - blocking failure with budget exhausted → RunItem `failed(qc_exhausted)` while retaining all versions;
   - external QC ambiguity → RunItem `uncertain` and reconciliation Job;
   - safe QC infrastructure error → existing QC Job `retry_wait` without creating duplicate check facts.
6. On certain quiescent success/failure, settle any QC attempt and release only the now-unused Reservation remainder.
7. Rebuild Run and active Operation projections inside the transition module.

QC never appends a human Approved review. A completed blocking failure remains permanent for that exact version.

## Reconciliation handlers

A platform scheduler may identify due uncertain ProviderAttempt IDs and Workspace IDs, but it does not mutate tenant domain rows or call Providers inline. It idempotently enqueues one `generation.reconcile_provider_attempt` Job per due attempt.

The per-attempt handler:

1. enters the recorded Workspace TenantContext/RLS scope;
2. claims a reconciliation lease so concurrent schedulers cannot duplicate lookup;
3. loads the frozen provider/request identity and the exact CredentialVersion evidence actually recorded on that attempt, when applicable;
4. performs provider-supported lookup with redacted evidence;
5. appends reconciliation events and follows Section 2:
   - output found → `processing_output` → AssetVersion → QC;
   - definitely never accepted/no charge → safe retry/failure according to remaining confirmed budget;
   - charge/no output → settle and fail according to evidence;
   - still unknown → remain uncertain and retain exposure;
6. updates projections through the reconciliation module.

Reconciliation retry uses the same Job's `retry_wait`; it does not issue concurrent lookup calls or blindly repeat the generation call.

## `generation.export_package` Worker handler

1. Load ExportPackage and its already frozen exact selector snapshot under TenantContext.
2. Revalidate Workspace lifecycle and the command's authorization epoch before beginning packaging. Download authorization will be checked again later.
3. Load exact AssetVersions, bindings, RecipeSnapshots, ProviderAttempts, QC, reviews, contextual verdict evidence, and cost evidence.
4. Stream each committed object through BlobStore, verify content hash/size, and fail closed on missing/forbidden/corrupt required content.
5. Generate traversal-safe deterministic filenames and canonical Manifest entries.
6. Deduplicate binary content by hash while retaining all Manifest business references.
7. Build ZIP with bounded resource usage; compute Manifest and ZIP hashes.
8. Upload the completed package through BlobStore and persist its internal committed reference/hash/size atomically with ExportPackage success evidence.
9. Transition Operation to succeeded with result reference `{resource_type: 'export_package', resource_id: export_package.id}`.

The handler never creates a signed URL or DownloadIntent.

## Legacy-path containment

- Existing legacy `/api/*` generation routes remain frozen; no new #21 feature is added there.
- All new BFF allowlist entries target `/api/v1/*` and use delegated TenantContext.
- New commands emit only the new Job types above.
- Existing `generate_asset` Jobs may drain through the legacy handler and compatibility projection.
- Legacy code cannot create new RunItem/ProviderAttempt state through a second implementation path.
- The contract migration defines a measurable removal gate: no queued/running legacy Jobs, migration evidence complete, and no supported caller emitting legacy work.

## OpenAPI and BFF deliverables

Implementation of this section must:

- declare explicit request/response/Problem Details models and unique `operation_id` values for every route;
- cap request bodies, `item_ids`, export selections, pagination limits, and timeline response sizes;
- regenerate and commit `contracts/openapi/marketai-domain-v1.json`;
- pass OpenAPI drift and breaking-change checks;
- regenerate the TypeScript client;
- expose use-case methods through the Web's deep `DomainGateway` interface rather than scattered `fetch` calls;
- update the BFF method/path allowlist and mechanically map `Location` headers;
- add authorization, cross-Workspace, idempotency, ETag/If-Match, pagination, redaction, and response-size contract tests.

---

# Section 4: Controlled Mock, Execution-Mode Isolation, and Provenance

## Contract ownership

Issue #18 owns the cross-cutting provider-selection, credential, usage, provider-cost, and Spend Guardrail contract. Issue #21 consumes that contract for the Generation → Assets slice; it does not redefine the global mode taxonomy.

Every generation path has exactly one frozen canonical #18 `execution_mode`:

```text
byok | sandbox | mock
```

`byok` means a real provider call using a Workspace-owned credential binding. `sandbox` means a Provider-official no-charge sandbox or explicitly no-charge Adapter. `mock` means the deterministic local Mock Adapter.

Issue #23 owns Meta-specific Sandbox behavior. It does not own the generic `sandbox` execution mode established by #18. Issue #21 does not have to invent a generation Sandbox Adapter where none exists, but it must preserve the mode, isolate its evidence, and reject Preview with `mode_unavailable` when the selected provider/model lacks that capability.

`real` and `controlled_mock` may appear only as presentation labels in explanatory UI copy. They are not persisted aliases and are never accepted where the #18 execution-mode enum is required. Existing `mock` values already use the canonical value and require no taxonomy migration.

## Execution-mode matrix

| Execution mode | Provider Adapter | Credential rule | Cost and Guardrail rule | Evidence class |
|---|---|---|---|---|
| `byok` | BYOK provider Adapter | An active eligible Workspace binding is required at Preview; the Worker records the actual CredentialVersion resolved at call time | Real provider quote, Reservation, Reported Cost or Estimated Settlement, Release/Adjustment, and Spend Guardrail exposure | Real provider evidence |
| `sandbox` | Provider-official or explicitly no-charge Sandbox Adapter, only when advertised | Only the Adapter-defined sandbox binding, if required; it cannot borrow an unrelated BYOK binding | No payable SpendReservation or provider-cost entry; usage remains isolated Sandbox evidence | Sandbox evidence |
| `mock` | `MockProviderAdapter` | No CredentialVersion; Mock cannot claim a BYOK or Sandbox credential | True payable is zero, no Spend Guardrail exposure, and any simulated estimate is separate mode/scenario evidence | Explicit synthetic evidence |

Provider failure never changes `execution_mode`. A new mode requires a new Draft/Preview/confirmation/RecipeSnapshot and therefore a new semantic intent.

## Mode resolution and freeze

1. `GenerationDraft` carries an explicit requested `execution_mode`; there is no implicit environment default at submit time.
2. ResolutionPreview resolves the requested mode through the `GenerationExecutionPolicy` module:
   - `byok` requires an active, eligible credential binding for the selected provider and capabilities;
   - `sandbox` requires an advertised Sandbox Adapter/capability and any Adapter-defined sandbox credential evidence;
   - `mock` requires a registered Mock Adapter/scenario and rejects any credential reference.
3. Preview freezes the effective mode, Adapter key/version, provider/model/capabilities, credential-binding identity and eligibility evidence, cost policy, and—where applicable—Mock scenario/version. It does not freeze a CredentialVersion.
4. `ResolutionPreviewConfirmation` confirms the exact Preview hash and quote. Confirmation cannot replace the mode or Adapter decision.
5. `GenerationRecipeSnapshot` copies the canonical resolved decision. Its mode and execution-policy evidence are immutable.
6. Workers resolve provider, model, mode, and binding identity only from the RecipeSnapshot and RunItem/ProviderAttempt identities. They never consult the mutable Draft or a current provider default.
7. Immediately before each external step, the Worker rechecks entitlement, rights, capability, and binding eligibility. For `byok` and credentialed `sandbox`, it resolves the binding's current active CredentialVersion and records the exact version on ProviderAttempt and cost evidence. Normal rotation does not change recipe semantics or require a new Preview. A missing/revoked binding pauses the item; changing provider, mode, binding identity, capability contract, or another frozen recipe input returns `new_preview_required`.

Public Preview/Run projections expose mode and non-sensitive Adapter/capability evidence. Exact CredentialVersion identity remains restricted evidence; secret plaintext, ciphertext, KMS material, and credential metadata inappropriate for the caller are never returned.

## The execution-policy module and Adapter seam

Mode-specific behavior is concentrated in one deep `GenerationExecutionPolicy` module. Its interface owns the facts callers would otherwise have to coordinate themselves:

```text
resolve_preview(draft, provider_selection, step_plan)
  -> resolved mode, Adapter/capabilities, credential rule/evidence,
     cost policy, scenario evidence, or a typed rejection

admit_external_step(snapshot, run_item, step_kind, current_tenant_evidence)
  -> Adapter selection, credential handle, cost-admission decision,
     redacted call policy, or a typed pause/rejection
```

This module hides the different credential, Adapter, cost, and downstream-eligibility rules. Callers and Workers do not scatter `if mode == ...` branches across the codebase.

All provider implementations sit at the same `UnifiedProviderAdapter` seam. At minimum, the interface provides:

```text
capabilities()
execute(call_request)
lookup(result_lookup_request)
```

The interface returns typed dispatch, response, usage, and lookup evidence. It does not update RunItem, Job, ProviderAttempt, cost, or AssetVersion rows. Those transitions remain owned by the existing domain modules and ExternalCallCoordinator.

The workflow is therefore shared, but mode policy is not pretended away: one execution-policy module selects and validates the correct Adapter/cost behavior, while all modes traverse the same durable state machines.

## Database and domain isolation invariants

For the new generation path, mode is stored on or immutably derived by:

- ResolutionPreview and ResolutionPreviewConfirmation;
- GenerationRecipeSnapshot, GenerationRun, and RunItem;
- every ProviderAttempt, including external visual QC;
- AssetQCAttempt and external QC evidence;
- UsageEvent, ProviderCostLedgerEntry, and SpendReservation where applicable;
- generation-origin AssetVersion and its RunItem binding;
- ExportPackage and every Manifest entry.

Copied mode fields are immutable integrity projections, not independent sources of truth. Tenant-safe foreign keys, checks, and insertion commands enforce:

- Run mode equals RecipeSnapshot mode;
- RunItem mode equals its Run mode;
- ProviderAttempt mode equals the frozen mode of its RunItem and step plan;
- generation-origin AssetVersion mode equals its producing ProviderAttempt/RunItem mode;
- QC evidence for an external QC step uses the same frozen generation mode;
- a `byok` attempt records exactly one CredentialVersion actually resolved immediately before that call;
- a `mock` attempt references no CredentialVersion;
- a `sandbox` attempt records a CredentialVersion only when the selected no-charge Adapter requires one;
- cost and Usage evidence use the attempt's mode and cannot be reclassified later.

If external visual QC is required, it must have an Adapter path for the same frozen mode. A `mock` generation cannot silently call a paid BYOK visual-QC Adapter. If no compatible external QC Adapter exists, Preview either selects a versioned deterministic local QC rule set or rejects the configuration before submit.

Reuse and deduplication fingerprints include at least:

- canonical RecipeSnapshot hash and canonicalization version;
- stable RunItem combination/variant identity and requested/effective Seed evidence;
- provider/model/capability and Adapter contract version;
- execution mode;
- Mock scenario/version and renderer/fixture version where applicable;
- reference and prompt evidence already required by the recipe contract.

Reuse is Workspace-scoped. A successful version from another execution mode is never a semantic reuse candidate, and the system never leaks whether another Workspace has equal content or fingerprints.

## Controlled Mock Adapter

`MockProviderAdapter` implements the same `UnifiedProviderAdapter` interface as BYOK and Sandbox Adapters.

### Frozen Mock identity

Controlled Mock evidence includes:

- `mock_adapter_key` and `mock_adapter_version`;
- registered `scenario_id` and `scenario_version`;
- deterministic renderer/fixture version;
- canonical semantic-input hash;
- attempt request hash and opaque provider request ID;
- requested/effective Seed evidence or an explicit Seed-unavailable reason.

Two hashes serve different purposes:

```text
semantic_input_hash = hash(
  canonicalization_version,
  recipe_hash,
  run_item_fingerprint,
  requested/effective Seed evidence,
  mock_adapter_version,
  scenario_id/version,
  renderer/fixture version
)

attempt_request_hash = hash(
  semantic_input_hash,
  ProviderAttempt identity,
  provider idempotency key
)
```

Synthetic output bytes derive from `semantic_input_hash`, so replaying the same frozen recipe/Seed/scenario/renderer is byte-deterministic. The opaque `provider_request_id` derives from the attempt identity/request hash through a Workspace-safe keyed derivation; it is not derived from Seed alone and does not expose cross-Workspace equality.

### Adapter behavior

The default success scenario:

- makes no network call;
- reads no CredentialVersion;
- incurs no true provider payable amount;
- emits deterministic image bytes and versioned metadata;
- accepts the persisted provider idempotency key;
- supports deterministic result lookup by the opaque provider request identity;
- returns typed usage/cost evidence with true payable zero and any simulated estimate separately tagged.

The Adapter may return `confirmed_dispatched` or `not_dispatched` when its call returns normally. That does not eliminate domain-level uncertainty: a Worker can still die after durable `dispatch_started` and before durable response evidence.

### Registered scenarios and fault injection

Mock scenarios are registered, versioned configurations, not arbitrary strings accepted from a browser. The normal customer-visible path uses `default_success`. Test/development or separately authorized diagnostic fixtures may include scenarios such as:

- definitive failure before dispatch;
- definitive transient/rate-limit failure;
- accepted call whose result is found by lookup;
- accepted call proven to have no result;
- temporarily unresolved lookup;
- malformed/corrupt output;
- missing provider-reported usage, requiring separate simulated-estimate evidence for Mock rather than a payable Estimated Settlement ledger entry;
- blocking visual-QC result.

Worker fault injection separately kills execution before dispatch, after `dispatch_started`, after Adapter return, after durable response, after output persistence, and during QC. Scenario and Worker fault injection are not conflated.

## Durable path fidelity and uncertainty

Controlled Mock uses the production path for everything except the concrete external Adapter implementation:

- real HTTP command/idempotency admission;
- real ResolutionPreview/confirmation/RecipeSnapshot freeze;
- real Operation, GenerationRunCommand, GenerationRun, RunItem, and Job records;
- real leases, heartbeat, retry scheduling, cancellation, and recovery;
- real ProviderAttempt header/event/projection writes;
- real `dispatch_started`, response, usage, and reconciliation evidence;
- real Asset/AssetVersion persistence and produced/reused bindings;
- real deterministic and mode-compatible QC;
- real review, regeneration, audit, Usage, export, and provenance paths;
- the same cost-module interface with mode-correct monetary effects.

The critical crash sequence remains:

```text
ProviderAttempt + dispatch_started committed
  -> Adapter call
  -> response evidence committed
```

If the Worker loses its lease or dies between the two commits, the ProviderAttempt/RunItem becomes `uncertain` under Section 2, even for a local Mock Adapter. `generation.reconcile_provider_attempt` performs lookup using the frozen request identity:

- result found → persist response/output idempotently, settle mode-correct evidence, then QC;
- definitely no result/no charge → safe retry or fail under the frozen attempt limits;
- still unresolved → retain `uncertain` and any applicable exposure.

For the default deterministic Mock scenario, reconciliation normally resolves immediately. Fault scenarios may deliberately retain uncertainty long enough to prove the same recovery interface and state transitions used by paid calls.

## Cost and Usage evidence by mode

All modes traverse the same cost/Usage module interface, but they do not have identical monetary effects.

### Real BYOK

- Quote and Reservation are created atomically for non-reused paid work.
- Reservation occupies real Spend Guardrail exposure.
- Reported Cost or Estimated Settlement references the exact ProviderAttempt.
- Release/Adjustment follows the evidence and uncertainty rules in Section 2.

### Sandbox

- The selected Adapter must satisfy #18's Provider-official or explicitly no-charge Sandbox contract.
- True provider payable and budget amounts are zero; Sandbox creates no payable SpendReservation or `provider_cost_ledger` entry.
- Usage remains explicit Sandbox evidence and never aggregates into `byok` or `mock` projections.
- A provider path capable of incurring payable cost is `byok`, not Sandbox, and must pass BYOK admission under a new Preview.

### Controlled Mock

- True provider payable and real budget amount are zero.
- It does not occupy real Spend Guardrail exposure.
- A simulated provider/budget estimate may be recorded only as separate `mock` scenario evidence.
- It creates no payable SpendReservation and no `provider_cost_ledger` entry, including zero-value placeholder reservations.
- ProviderAttempt/Usage evidence still records the synthetic call and output facts, preserving the real idempotency and recovery path.

Cost projections group by execution mode, currency, evidence type, RunItem, and ProviderAttempt. No query obtains a total by adding Reported Cost, Estimated Settlement, simulated estimates, and Reservations together.

## Provenance model

Provenance is queryable through normalized immutable identities. AssetVersion does not copy an unstructured mega-document that competes with those records.

### Intent and creation chain

```text
GenerationDraft
  -> ResolutionPreview
       -> ResolutionPreviewConfirmation (actor + exact quote/hash)
       -> canonical content -> GenerationRecipeSnapshot

ResolutionPreviewConfirmation
  + GenerationRecipeSnapshot
  + creation Operation
  -> GenerationRun
       -> initial GenerationRunCommand
```

RecipeSnapshot and Confirmation are siblings derived from the Preview. RecipeSnapshot is not owned by Confirmation, and neither is rewritten by later retry/resume commands.

### Command and execution chain

```text
GenerationRun
  -> GenerationRunCommand (submit/retry/resume/cancel/reconcile)
       -> Operation
       -> target selection / recovery confirmation where required
  -> RunItem
       -> Job history
       -> ProviderAttempt
            -> ProviderAttemptEvents
            -> actual internal CredentialVersion identity where required
            -> response/usage evidence
            -> ProviderCostLedgerEntry / SpendReservation evidence
       -> RunItemAssetVersionBinding
```

Every produced attempt is attributable to the command/recovery cycle that authorized it. Later commands never overwrite the creation Operation or earlier attempt history.

### Production and reuse chains

The consuming chain and original production chain are distinct:

```text
Export/business reference
  -> consuming RunItem
       -> binding(kind=produced|reused)
            -> AssetVersion

AssetVersion
  -> original producing RunItem
       -> original producing ProviderAttempt
       -> original GenerationRun / RecipeSnapshot / creation intent
  -> AssetQCAttempt -> AssetQCCheckResults
  -> QualityReview history
  -> optional parent AssetVersion (regeneration/edit lineage)
```

For `kind=produced`, the consuming and producing RunItem are the same. For `kind=reused`, the later RunItem points to the existing AssetVersion without becoming its producer; provenance follows the source version's original producing chain. Reuse does not clone, rewrite, or obscure original cost/QC/review evidence.

ProviderAttempt events contain dispatch/response/usage/reconciliation facts. Monetary settlement remains a separate cost-ledger chain referencing the ProviderAttempt; it is not misrepresented as a mutable ProviderAttempt event.

## Visible and redacted provenance

Authorized Run/Asset projections and the Manifest expose enough redacted evidence to answer:

- who requested the intent and which command/recovery cycle produced the result;
- which immutable Draft resolution, Preview confirmation, and RecipeSnapshot were used;
- which RunItem combination/variant, requested/effective Seed, provider/model/API/Adapter version, and execution mode were used;
- whether the version was produced or reused, and where the original production chain lives;
- which ProviderAttempt, QC checks, human reviews, parent version, and contextual verdict references apply;
- which Quote/Reservation/Reported/Estimated/Release/Adjustment or simulated cost evidence applies.

Internal evidence stores the exact CredentialVersion ID resolved for each BYOK/Sandbox call when required by #18. Generic Member projections and exports expose only authorization-appropriate redacted credential provenance; they never expose secret plaintext, ciphertext, KMS references, signed URLs, raw Provider payloads, arbitrary internal object keys, or cross-Workspace identifiers.

## Export isolation

An Issue #21 ExportPackage has exactly one frozen generation execution mode.

- A GenerationRun selector naturally resolves to its Run's single frozen mode.
- An exact AssetVersion selector is rejected with `409 mixed_execution_modes` when selected generation-origin versions do not share a mode.
- The caller creates separate packages for `byok`, `sandbox`, and `mock` material.
- Manifest entries still repeat mode explicitly; package-level mode is never inferred from filenames or provider names.
- Content-hash deduplication happens only inside the authorized package and never reveals cross-Workspace or cross-mode equality.
- Cost, QC, review, or contextual verdict summaries never aggregate across modes.

## Downstream safeguards

Issue #21 records generation provenance; downstream tickets enforce their own eligibility rules without rewriting it.

- **Experiments (#22):** all candidate AssetVersions in one frozen candidate set must share the same generation execution mode. Experiment/Meta observation mode is separate evidence; #22 must retain both lineages and must never promote a Mock/Sandbox-dependent conclusion into real evidence.
- **Meta read and PAUSED Launch (#23):** Meta `sandbox`, `mock`, and internal `real_read_only` are #23's own external-evidence modes. A generation AssetVersion's mode is not silently replaced by the Meta connection mode.
- **Localization (#24):** the source AssetVersion retains its generation mode. LocalizationBatch/Attempt records its own explicit mode. A localized AssetVersion carries both source and localization lineage and cannot cross-promote evidence.
- **Scale (#25):** Enrollment/Evidence/Advice keeps real-read-only, Sandbox, and Mock evidence isolated. Scale is #25, not #23.

The downstream effective-evidence rule is conservative: derived evidence must retain every contributing mode and cannot be promoted above its least-production provenance. Exact combination semantics belong to the owning downstream ticket, not to #21.

## Section 4 invariants

- #21 inherits `byok | sandbox | mock` from #18; Meta Sandbox is not the generic mode owner.
- Mode, Adapter/capability version, and applicable scenario evidence are frozen before submit.
- Provider failure, credential change, retry, resume, QC feedback, and reconciliation never change mode.
- One execution-policy module owns mode-specific credential, Adapter, cost, and admission rules; Workers share one durable workflow.
- Mock replaces only the external Adapter implementation and has zero true payable/real Guardrail effect.
- A Worker durability gap may produce `uncertain` in every mode; only reconciliation resolves it.
- Mock output/request identity is versioned and canonical; Seed alone is never a request identity.
- Generation/external-QC attempts for a RunItem remain mode-compatible.
- Reuse never crosses mode and never rewrites the original producer.
- Production, consumption/reuse, command/recovery, QC/review, parent-version, and cost chains remain independently queryable.
- ExportPackage is single-mode and manifests are explicit, deterministic, redacted, and secret-free.
- Downstream systems preserve generation mode alongside their own evidence modes and cannot cross-promote derived evidence.

---

# Section 5: Migration Strategy, Phased Rollout, and Automated Evidence

## Migration and rollout principles

Issue #21 uses expand → cut over → drain → backfill → verify → constrain → later contract. Schema authoring order, implementation order, and production deployment order are related but are not the same thing.

The governing rules are:

- All schema changes use reviewed Alembic revisions. Production never uses schema push.
- PostgreSQL composite foreign keys, roles, grants, and RLS are authoritative. SQLite remains a supported local/test Adapter where the repository supports it, but it does not weaken hosted PostgreSQL guarantees.
- There is one generation write interface. The migration does not create permanent dual writers or two mutable sources of truth.
- New commands create only the Section 1 identities and `generation.*` Jobs. The old handler may drain already-existing `generate_asset` Jobs but cannot accept new work after cutover.
- Additive schema remains compatible until cutover. Constraints are not tightened while a supported legacy writer can still create incomplete rows.
- Backfill is deterministic, idempotent, restartable, bounded in batches, and auditable. It never invents missing user confirmation, Seed, ProviderAttempt, credential, QC, usage, or reported-cost evidence.
- Once new-format writes exist, rollback means disabling new intake and rolling forward with the new reader/recovery implementation. It does not mean deploying an old application that cannot understand new Runs.
- Destructive contract migrations occur only in a later release after an observed rollback window and explicit removal gates.

## PostgreSQL and SQLite responsibilities

PostgreSQL migrations must create tenant-safe parent uniqueness, composite foreign keys, indexes, grants, and RLS policies. Every new tenant table with a direct `workspace_id` receives `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, `USING`, and `WITH CHECK` policy coverage in the same expand revision that makes the table available to application roles.

SQLite migrations use Alembic batch mode where required and preserve foreign-key behavior supported by SQLite. SQLite tests exercise command interfaces, conditional constraints that the dialect can represent, projection rebuilds, and migration compatibility. They do not stand in for PostgreSQL tests of RLS, actual application/dispatcher/worker roles, composite-mode foreign keys, concurrent indexes, or PostgreSQL constraint validation.

Dialect-specific implementation is allowed behind the persistence Adapter. The domain interface and invariants remain identical.

## Production migration sequence

### D0: Preflight and rollback preparation

Before applying schema changes:

- verify a single expected Alembic head and record the deployed application/schema versions;
- take and verify the applicable database backup/restore point;
- inventory GenerationRun, Asset task-row, AssetVersion, QualityReview, SpendReservation, ProviderCostLedger, legacy `cost_ledger`, ProviderCallAttempt, and legacy Job counts by state and Workspace;
- record counts for queued, leased/running, retry-wait, and uncertain `generate_asset` Jobs;
- verify no previous interrupted #21 migration/cutover is active;
- establish bounded lock/statement timeouts and backfill batch size;
- run the migration verifier in read-only dry-run mode and persist its summary/checksum as deployment evidence.

Any failed preflight aborts before schema or write-path mutation.

### M1 / D1: Expand and deploy dark-compatible code

The expand revisions create the new schema and add nullable compatibility columns without breaking the active legacy writer.

#### New domain tables

- `generation_drafts`
- `resolution_preview_confirmations`
- `generation_recipe_snapshots`
- `run_items`
- `provider_attempt_events`
- `provider_attempt_projections`
- `run_item_asset_version_bindings`
- `asset_qc_attempts`
- `asset_qc_check_results`
- `generation_run_commands`
- `export_packages`

Each table receives the Section 1 mutability protections, tenant-safe identity/indexes, and PostgreSQL RLS/grants before an application role can access it. `provider_attempt_projections` remains a mutable CAS projection; immutable protections apply to headers/events and the other append-only/write-once evidence tables, not to the rebuildable projection.

#### Additive changes to existing tables

- `resolution_previews`: add nullable `purpose` and the nullable initial-generation/recovery owner fields needed by the two typed purposes. New #21 writes require an explicit purpose; no blanket default relabels unknown historical rows.
- `generation_runs`: add nullable `recipe_snapshot_id`, `creation_preview_confirmation_id`, `creation_operation_id`, `submission_kind`, Section 2 count/state projections, and any needed revision/timestamps.
- `asset_versions`: add nullable `producing_run_item_id`, `provider_attempt_id`, generation origin/provenance kind, recipe projection/reference where specified, and requested/effective Seed evidence or Seed-unavailable reason.
- `quality_reviews`: add nullable `asset_version_id`, `legacy_unscoped`, and nullable `legacy_asset_id`. No XOR constraint is enabled until backfill is complete.
- `provider_call_attempts`: evolve the #18 identity in place with nullable RunItem/step/input-version fields and request evidence; make SpendReservation linkage conditionally nullable for canonical `mock` and no-charge `sandbox`. Existing dispatch/outcome columns remain temporary compatibility projections.
- `spend_reservations`: add nullable RunItem and Preview/RecipeSnapshot provenance required by new paid generation rows.
- `provider_cost_ledger`: add nullable `run_item_id`, `provider_call_attempt_id`, confirmation/snapshot, and reconciliation attribution required by new generation entries. Preserve the #18 entry-type, currency, direction, mode, and semantic-key rules.
- legacy `cost_ledger`: no schema or data rewrite; retain the #18 read-only compatibility bridge.

Existing task columns and legacy indexes remain. Columns added to populated tables avoid nonessential rewrite-producing defaults; new-write requirements are enforced first through the new command interface and later through M3 constraints.

The dark-compatible application release includes:

- the new domain modules and readers behind disabled mutation routing;
- one read/projection module capable of presenting new rows and explicit legacy compatibility rows;
- the unchanged legacy drain handler for already-existing Jobs;
- migration dry-run/verifier commands;
- telemetry for old/new row counts, null/new-path violations, Job-type creation, and projection parity.

It does not dual-write a legacy Asset task row and a new RunItem for one new command.

### D2: Atomic write cutover and legacy drain

Cutover changes command routing so exactly one intake path is enabled:

```text
new Generation command intake: enabled
legacy create_run / generate_asset Job creation: disabled
legacy handler for already-existing Jobs: enabled for drain only
```

The cutover gate verifies that every supported caller has moved to the Section 3 HTTP/domain interface and that unknown legacy callers fail explicitly rather than silently creating old work.

After cutover:

- all new commands atomically create RecipeSnapshot, confirmation, Operation, GenerationRunCommand, Run, RunItems, applicable cost/capacity evidence, and new Job types;
- new Jobs use exact payload versions and `resource_type='run_item'` where applicable;
- the old handler may finish only Jobs that existed before the cutover watermark;
- no legacy handler writes new ProviderAttempt events/projections or RunItem state through a second implementation path;
- the compatibility reader continues to show old and new history without treating legacy task Assets as new output identities.

The drain gate requires zero queued, leased/running, and retry-wait legacy generation Jobs. Permanently quiescent failed/cancelled/uncertain legacy evidence may remain for truthful compatibility backfill; it is never blindly retried as a new paid call.

### M2 / D3: Deterministic backfill and catch-up verification

Backfill runs only after legacy Job creation is disabled and the drain gate is satisfied. It processes stable primary-key ranges in bounded transactions. Rerunning a completed or interrupted batch produces the same IDs and never duplicates evidence.

#### Deterministic identity rules

- Backfilled IDs use a fixed migration namespace plus Workspace, source table, and legacy source ID.
- Every backfilled row stores or is covered by a deterministic legacy-source identity/uniqueness rule.
- Canonical JSON uses a fixed `legacy-v0` canonicalization implementation.
- Each legacy Run snapshot includes its legacy Run identity in the canonical legacy envelope, preventing incomplete historical Runs from being incorrectly content-deduplicated.
- Backfill emits an audit summary and per-row evidence for exceptional/ambiguous decisions without logging prompts, object keys, or secrets.

#### RecipeSnapshot backfill

For each legacy GenerationRun:

- construct one deterministic `legacy-v0` snapshot using only available Run, Plan/combination, Asset task, Prompt, provider/model, format, reference, and cost evidence;
- retain raw historical identifiers/hashes needed to explain the reconstruction;
- mark every missing field explicitly in structured `legacy_incomplete_fields` evidence;
- use explicit execution mode only when historical evidence proves it; recognized legacy `mock` remains canonical `mock`, while unprovable mode remains legacy-incomplete rather than being guessed;
- set `submission_kind='legacy_migrated'` and leave creation confirmation/Operation absent unless a genuine historical Operation exists and the contract explicitly permits linking it;
- never create a fake ResolutionPreviewConfirmation.

#### RunItem backfill

Create one deterministic RunItem for each legacy Asset that acted as a task row. Preserve `legacy_asset_id`, combination identity/tags, raw legacy state, errors, timestamps, and available fingerprint evidence.

State mapping is evidence-based:

| Legacy evidence | Backfilled RunItem result |
|---|---|
| `ok` plus a durably committed, unambiguous AssetVersion | `succeeded` with produced binding |
| explicit reuse plus an unambiguous source AssetVersion | `reused` with reused binding; the source producer is unchanged |
| explicit terminal failure | `failed` with typed legacy failure provenance |
| explicit cancellation | `cancelled` |
| nonterminal/inconsistent evidence after the drain gate | `paused` with `legacy_recovery_required` or `failed(legacy_evidence_incomplete)` according to whether a safe local recovery remains possible |

Backfill does not fabricate ProviderAttempt history to justify a state. Missing Seed becomes an explicit Seed-unavailable reason. Incomplete legacy fingerprints are marked `reuse_eligible=false`; they cannot accidentally satisfy new semantic reuse.

#### AssetVersion and binding backfill

- Link `producing_run_item_id` and append a `produced` binding only when the legacy Asset/RunItem is the unambiguous producer of that exact version.
- For explicit legacy reuse, append a `reused` binding from the consuming RunItem to the source version without changing the version's original producer.
- Where production cannot be proved, leave generation provenance nullable under the legacy/origin-kind rule and retain the legacy identity.
- Never create a synthetic AssetVersion merely to make an old successful-looking task row satisfy a constraint.

#### QualityReview backfill

- Map a historical review to `asset_version_id` only when exactly one AssetVersion for that Asset existed at `review.created_at`.
- If zero or multiple versions existed, preserve it as `legacy_unscoped=true` with `legacy_asset_id`; do not pick a version by ID or timestamp tie-break.
- Record the candidate count and mapping reason in migration audit evidence.
- Legacy-unscoped reviews remain visible as history but never enter current exact-version review or approval projections.

#### ProviderCallAttempt enrichment

Existing #18 `provider_call_attempts` rows retain their IDs. Backfill `run_item_id`, `step_kind`, and related input/output provenance only when the Operation/Job/legacy Asset evidence proves one unambiguous mapping. Otherwise preserve the attempt as pre-#21 evidence with explicit incomplete provenance. Append one deterministic `legacy_state_imported` event from the row's recorded state/timestamps/evidence and source hash, then build its compatibility projection from that event; do not synthesize a stronger `dispatch_started` or certainty claim than the source proves. Never copy an attempt into another header table, invent a CredentialVersion, or rewrite its recorded dispatch truth.

#### Legacy cost compatibility verification

Historical legacy `cost_ledger` rows remain single-counted facts through the existing #18 read-only bridge:

- preserve original amount, currency, timestamp, source identifiers, note/hash, and legacy entry type;
- normalize legacy `reserved`/`released` semantics through the compatibility projection as Reservation/Release evidence;
- treat the historical `actual` value as legacy estimated settlement because the current implementation derived it from configured unit price rather than Provider-reported usage;
- mark its legacy evidence classification explicitly and never expose it as Reported Cost;
- do not append a second monetary row that would make both the old and normalized fact enter totals;
- expose RunItem/ProviderAttempt/mode provenance only when it can be derived without changing the legacy row or fabricating identities.

No backfill copies these rows into `provider_cost_ledger`. New monetary facts use only `spend_reservations` and `provider_cost_ledger`; the combined read projection preserves a source discriminator and prevents duplicate aggregation.

#### Backfill verification

Before M3, the verifier must prove:

- every legacy Run has exactly one deterministic RecipeSnapshot link;
- every legacy Asset task row has exactly one deterministic RunItem;
- every enriched ProviderCallAttempt retains its original ID and has either one proved RunItem mapping or explicit incomplete provenance;
- every binding is tenant-safe and its produced/reused semantics are internally consistent;
- every historical review is either truthfully version-scoped or explicitly legacy-unscoped;
- original ledger amount/currency totals and per-entry hashes are preserved without double counting;
- no backfill row claims confirmation, Seed, ProviderAttempt, credential, usage, QC, or reported cost absent from history;
- rerunning the backfill yields zero new/changed rows;
- no active legacy generation Job exists beyond the recorded drain exception set;
- sampled and aggregate compatibility reads match pre-cutover historical projections where semantics are comparable.

Any mismatch blocks M3. The repair is another deterministic backfill/verifier release, not manual row editing.

### M3 / D4: Constrain and make the new path authoritative

M3 executes only after the new writer is live, legacy creation is disabled, drain/backfill is complete, and verification passes.

PostgreSQL constraints are staged to avoid unnecessary long blocking operations: create/validate eligible `NOT VALID` checks and foreign keys, build large indexes with the appropriate migration transaction handling, then apply final nullability/uniqueness. SQLite uses an equivalent batch rebuild where supported.

M3 enforces:

- `generation_runs.recipe_snapshot_id IS NOT NULL` after truthful backfill;
- `submission_kind='confirmed_preview'` requires `creation_preview_confirmation_id` and `creation_operation_id`;
- `submission_kind='legacy_migrated'` cannot claim a fabricated confirmation;
- unique initial confirmation consumption on GenerationRun and unique confirmation reference on GenerationRunCommand;
- a PostgreSQL deferrable constraint trigger verifies that every confirmed-preview Run has exactly one `submit` GenerationRunCommand whose Operation and confirmation equal the Run's creation fields; SQLite and all callers enforce the same invariant through the command interface/verifier;
- the same deferred invariant verifies that initial confirmations belong to `initial_generation` Previews and recovery-command confirmations belong to matching `run_recovery` Previews;
- exact QualityReview provenance XOR, including required `legacy_asset_id` for legacy-unscoped rows;
- new-row `spend_reservations`/`provider_cost_ledger` reference, direction, currency, and mode rules while retaining the read-only legacy compatibility bridge;
- generation-origin AssetVersion requires producing RunItem, ProviderAttempt, recipe/mode, content, and Seed-or-unavailable evidence;
- manual/imported/localized/legacy AssetVersion origins retain their legitimate conditional nullability;
- append-only/write-once protections for snapshots, confirmations, ProviderAttempt headers/events, bindings, QC results, reviews, and immutable output evidence;
- ProviderAttempt event uniqueness by `(workspace_id, provider_call_attempt_id, sequence)` and semantic event idempotency key;
- RunItem uniqueness by `(workspace_id, generation_run_id, ordinal)` and semantic fingerprint claims used by submission/reuse;
- RecipeSnapshot uniqueness by `(workspace_id, canonicalization_version, recipe_hash)`;
- ExportPackage single-mode membership.

Cross-row mode consistency is not expressed as a PostgreSQL `CHECK` that illegally queries another table. Where mode is copied for integrity/performance, the parent exposes a unique identity including mode and the child uses a composite foreign key such as:

```text
(workspace_id, generation_run_id, execution_mode)
  -> generation_runs(workspace_id, id, execution_mode)

(workspace_id, run_item_id, execution_mode)
  -> run_items(workspace_id, id, execution_mode)
```

Local `CHECK` constraints enforce only row-local enum, credential-nullability, amount/direction, and origin rules.

RLS is already enabled from M1. D4 re-verifies policies/grants using real application, dispatcher, worker, and migration roles; it does not postpone tenant isolation until this phase.

### M4 / D5: Later contract removal

M4 is a later production release, not part of the initial #21 cutover or its rollback path. It runs only after all of the following remain true for the agreed observation window:

- no supported caller reads or writes deprecated columns;
- static/runtime telemetry shows zero legacy mutation route and Job creation;
- no queued/running/retry-wait legacy generation Jobs remain;
- compatibility backfill/verifier is green and retained history is queryable;
- the current and immediately previous supported application releases understand the constrained schema;
- backup/restore and forward-recovery procedures are verified;
- an explicit migration review approves each destructive target.

Eligible later removals include legacy Asset task `status`/task-error fields and GenerationRun provider/model/aspect-ratio/image-size fields whose authoritative source has moved to RecipeSnapshot.

`Asset.run_id` is not dropped as a task-field cleanup. It remains immutable legacy origin-run provenance as specified in Section 1, or is renamed/moved only by a separately reviewed provenance migration that preserves its meaning.

Historical cost compatibility and legacy-unscoped reviews remain as long as retention/audit requirements require them. M4 never fabricates a reversible downgrade after destructive data removal; rollback after this point is forward restoration from verified evidence/backup.

## Implementation capability phases

These phases describe how the code and tests are built. They do not authorize applying M2/M3/M4 before the production gates above.

### Phase 1: Expand schema, domain identities, and migration tooling

Scope:

- author M1 expand revisions, PostgreSQL RLS/grants, composite tenant identities, indexes, and ORM/domain models;
- implement deterministic backfill dry-run/execution/verifier tooling without applying production cutover;
- implement the `GenerationExecutionPolicy` interface skeleton and Adapter registry;
- implement the deep ProviderAttempt module by evolving the existing `provider_call_attempts` header and adding events/projection;
- implement immutable/append-only persistence protections and projection rebuild;
- add compatibility readers while leaving the new mutation route disabled.

Automated evidence:

- migrate from the repository's supported predecessor head and from an empty database on PostgreSQL and SQLite;
- `alembic check`/metadata drift verification;
- pre-write downgrade tests only; tests explicitly prove that post-write destructive downgrade is unsupported;
- new-table RLS tests using real PostgreSQL application/dispatcher/worker roles, including missing TenantContext and forged cross-Workspace IDs;
- composite-FK and conditional-origin negative tests;
- backfill dry-run determinism, batching, resume, and idempotency fixtures;
- ProviderAttempt projection rebuilt from events equals the incremental projection byte-for-byte;
- immutable roles cannot update/delete evidence while the privileged erasure path remains separately controlled/audited.

### Phase 2: Draft → confirmed Preview → frozen RecipeSnapshot

Scope:

- GenerationDraft create/read/update/archive commands with CAS;
- initial and recovery ResolutionPreview creation;
- atomic submission creating Confirmation, Snapshot, Operation, initial GenerationRunCommand, Run, RunItems, applicable Reservation/capacity evidence, and Jobs;
- semantic reuse claims under concurrency;
- Section 3 HTTP routes, Problem Details, OpenAPI, BFF policy/allowlist, and generated bindings;
- durable HTTP idempotency reservation/finalization.

Automated evidence:

- stale Draft revision and concurrent CAS rejection;
- full selector resolution, typed SkipReasons, no page-truncation hash error, expiry, and dependency/credential/capability/quote invalidation;
- full planning selects every feasible candidate and fails rather than pruning when its frozen limit is exceeded;
- coverage planner v1 deterministically satisfies all feasible strength-1/strength-2 obligations, uses fingerprint tie-breaks, and fails with uncovered obligation hashes when the frozen budget is insufficient;
- repeated planning from canonically equivalent inputs produces identical ordered fingerprints, SkipReason partitions, quote, Seeds, and Preview hash;
- Confirmation actor/hash/quote evidence and one Preview → one submitted intent under different HTTP keys;
- Snapshot update/delete rejection and uniqueness on `(workspace, canonicalization_version, recipe_hash)`;
- atomic all-or-nothing submission at every transaction failure point;
- same key/same request replay returns the existing Operation; same key/different fingerprint returns `409 idempotency_key_reused`;
- concurrent different keys for one Preview create one Run only;
- fingerprint match creates `reused` binding with no Job/Reservation; nonmatch creates new work;
- cross-Workspace/role/authorization epoch/entitlement/Guardrail negative tests;
- OpenAPI drift, BFF generated validator/allowlist, Location, response-validation, and size-limit tests.

### Phase 3: ProviderAttempt, cost, controlled Mock, and recovery to QC handoff

Scope:

- `generation.run_item` Worker handler through durable AssetVersion persistence and `qc_pending` handoff;
- full `GenerationExecutionPolicy` implementation;
- BYOK and controlled-Mock Adapters plus explicit no-charge `sandbox` capability/unavailable behavior;
- ProviderAttempt dispatch/response/failure/usage/certainty/reconciliation lifecycle;
- atomic BYOK SpendReservation/settlement/release/adjustment integration through #18's authoritative tables;
- automatic safe retry on the same Job row;
- pause, cancel/drain, manual retry, resume, recovery Preview, and reconciliation commands;
- versioned Mock scenarios and Worker fault-injection hooks.

Automated evidence:

- success commits `dispatch_started`, response, output, produced binding, Usage, settlement evidence, and QC Job exactly once, ending this phase's item assertion at `qc_pending`;
- definitive transient failure retries within frozen limits without reusing a Job attempt as a ProviderAttempt;
- definitive terminal failure settles/releases correctly and becomes failed;
- crash after `dispatch_started` but before durable response becomes `uncertain`; retry/resume is rejected until reconciliation;
- lookup found/no-result/still-unknown reconciliation outcomes;
- Mock uses the same durable path, can become uncertain at the durability gap, records scenario/version, has zero true payable, and has no real Guardrail effect;
- Sandbox selection returns `mode_unavailable` when no compatible Adapter exists and never falls back;
- mode/credential matrix and cross-mode reuse rejection;
- uncertain exposure is held; definitely unused exposure alone is released;
- temporary same-snapshot gate failure can pause/resume after revalidation;
- rotation to a new eligible CredentialVersion inside the same binding remains same-snapshot resumable and records the actual version per attempt; a missing/revoked binding pauses, while changing binding identity requires a new Preview;
- cancellation races before dispatch, after dispatch, after response, and after output persistence preserve truthful output/cost evidence;
- manual retry creates a new Operation/Job for eligible failed items only; uncertain/succeeded/reused/cancelled selections are rejected;
- unknown Job payload versions fail before side effects.

### Phase 4: AssetVersion QC and exact-version human review

Scope:

- `generation.qc_asset_version` Worker handler;
- deterministic and mode-compatible external visual QC;
- AssetQCAttempt/AssetQCCheckResult evidence and bounded feedback;
- RunItem closure from `qc_pending` to succeeded, feedback, failed, or uncertain;
- append-only QualityReview commands on an exact AssetVersion;
- Asset/version/QC/review query interfaces and noncontextual `ready_for_experiment` projection.

Automated evidence:

- AssetVersion persists even when QC fails;
- blocking failure permanently prevents Approved for that exact version;
- nonblocking failures do not masquerade as human Approved;
- external visual QC uses ProviderAttempt `step_kind=visual_qc`, the frozen compatible mode, and the same uncertainty/reconciliation interface;
- feedback creates a new ProviderAttempt and child AssetVersion while preserving prior versions/results;
- feedback/attempt/exposure caps terminate as `failed(qc_exhausted)` without deleting output;
- QualityReview is append-only, exact-version scoped, CAS/idempotent, and rejects Approved with blocking QC;
- legacy-unscoped reviews never affect current-version review or eligibility;
- `ready_for_experiment` requires exact-version Approved, no blocking QC, rights/dependency eligibility, and mode provenance, but creates no Experiment verdict;
- Run/Operation projection closes correctly only after QC reaches a quiescent result.

### Phase 5: Regeneration intents

Scope:

- `create_regeneration_draft` from an exact source AssetVersion;
- `same_recipe`, `new_seed`, `edit_prompt`, and `quality_feedback` intent rules;
- pinned source recipe/version provenance and exact QC feedback reference;
- the full new Preview/confirmation/submission cycle;
- new output versions linked through `parent_version_id`.

Automated evidence:

- each intent pins/copies only the fields its contract allows and never mutates the source;
- `quality_feedback` requires a QCAttempt belonging to the exact source version;
- `same_recipe` preserves Prompt/Seed/model/parameters but follows the provider determinism disclosure;
- `new_seed` changes explicit Seed evidence and semantic fingerprint;
- `edit_prompt` creates new prompt evidence rather than rewriting RecipeSnapshot;
- every regeneration is a new intent/Operation/Run path, not a retry, and cannot skip Preview, confirmation, cost admission, QC, or review reset;
- parent-version chains remain acyclic and fully queryable.

### Phase 6: ZIP/Manifest export and authorization

Scope:

- single-mode ExportPackage creation with frozen exact selector/context;
- `generation.export_package` Worker handler;
- deterministic ZIP/Manifest construction, content-hash deduplication, and committed object evidence;
- fresh DownloadIntent creation with current authorization revalidation;
- a contextual-verdict query interface for `winner` selection without owning Experiment truth.

Automated evidence:

- `all_successful` and `approved` selectors freeze exactly eligible versions;
- `winner` requires authoritative #22 Experiment/context evidence; absent evidence returns a typed unavailable/ineligible result and never fabricates Winner;
- mixed generation-mode version selection returns `409 mixed_execution_modes`;
- Manifest contains production and reuse chains, recipe/preview/command/attempt, Seed availability, QC/review, mode, cost, and hashes without secrets;
- identical bytes create one file and multiple Manifest business references;
- canonical Manifest order/serialization plus normalized ZIP entry order, timestamps, permissions, filenames, and compression settings produce the same hashes for the same frozen input;
- paths are traversal-safe, normalized, collision-resolved, and length-bounded;
- missing/corrupt/forbidden required object evidence fails closed;
- `include_failed_evidence` adds only metadata for failed/uncertain items and never fake files;
- a Run with uncertain siblings may export its eligible successful versions; uncertain evidence appears only when explicitly requested;
- DownloadIntent revalidates current Workspace membership/authorization and never exposes internal object keys;
- cross-Workspace selection/download and stale authorization epoch tests fail without existence leakage.

### Phase 7: End-to-end, cutover readiness, and fault matrix

Scope:

- end-to-end domain test from Library versions through controlled-Mock generation, QC, review, and export;
- all command, Worker, persistence, object-store, cost, and authorization failure matrices;
- migration dry-run/backfill/verifier rehearsals on production-shaped fixtures;
- static/runtime proof that new callers cannot create legacy Jobs;
- complete OpenAPI/BFF/generated-artifact and `make verify` gates;
- D0–D4 runbook and rollback/forward-recovery rehearsal.

The #21 golden path ends at an Approved, noncontextually `ready_for_experiment` AssetVersion plus a valid ExportPackage. It does not create an ExperimentDecision or statistical Winner; #22 owns those facts.

Automated evidence includes:

- empty Workspace → publish Library versions → Draft → Preview → submit → controlled Mock → durable output → QC pass → human Approved → export/download intent;
- partial siblings remain usable: success + failure + uncertainty projects Run `uncertain` while unresolved, exposes the successful version, then becomes the correct terminal projection after reconciliation;
- cancel scenarios distinguish all-cancelled, partially succeeded after cancellation, and completed-after-cancel output;
- manual retry targets only eligible failed items and preserves creation/recovery Operations;
- temporary gate pause/resume and revoked-credential `new_preview_required` paths;
- second equivalent submission reuses successful output with no Provider call or cost;
- QC feedback produces a child version and bounded terminal result;
- Worker kill matrix:
  - before `dispatch_started` → safe Job re-lease/requeue, no ProviderAttempt consumed;
  - after `dispatch_started`, before durable response → `uncertain`;
  - after durable response, before output persistence → local idempotent replay, no Provider recall;
  - after output persistence, before QC enqueue/claim → idempotent QC recovery;
  - during deterministic/local QC → safe resume;
  - during external QC after dispatch → `uncertain` plus reconciliation;
- per-reservation cost projection proves:

```text
authorized exposure
  = authoritative settled amount
  + released amount
  + open available exposure
  + held uncertain exposure
```

  where directional Adjustments are applied exactly once and Reported Cost supersedes—not adds to—the matching Estimated Settlement projection;
- concurrent submit/reuse/cost tests prove one semantic provider/cost result;
- every command and Worker gate covers cross-Workspace IDs, forged/missing TenantContext, revoked membership, stale authorization epoch, expired entitlement lease, mode mismatch, and withdrawn dependency;
- PostgreSQL RLS and application-interface isolation agree under direct negative queries;
- projection rebuild/restart produces the same Run, Operation, ProviderAttempt, QC, cost, and export state.

## Database invariants after M3

After M3, the database and domain interfaces jointly enforce:

1. Every tenant row has a valid Workspace identity and every cross-table tenant reference is composite.
2. RLS is enabled and forced on every new PostgreSQL tenant table with read/write checks for the correct roles.
3. Normal application/dispatcher/worker roles cannot update/delete immutable or append-only evidence.
4. Every GenerationRun references a RecipeSnapshot; confirmed-preview Runs also reference their unique creation confirmation and Operation.
5. Initial/recovery confirmation purpose and unique-consumption rules agree with GenerationRunCommand/Operation evidence.
6. Every new RunItem is the sole task identity and is unique within its Run ordinal/fingerprint rules.
7. ProviderAttempt request identity is immutable; events have monotonic unique sequence and semantic idempotency; projection rebuild is deterministic.
8. New generation-origin AssetVersion rows require exact producer/attempt/mode/content/Seed evidence; legitimate non-generation and truthful legacy origins remain conditionally nullable.
9. Produced/reused bindings are append-only and cannot rewrite an AssetVersion's original producer.
10. QualityReview has exactly one provenance form: exact-version or explicitly legacy-unscoped.
11. New cost entries satisfy type/reference/direction/currency/mode rules; legacy values are explicitly classified and counted once.
12. Cross-row mode integrity uses composite foreign keys or derivation, never an invalid cross-table `CHECK`.
13. RecipeSnapshot uniqueness is `(workspace_id, canonicalization_version, recipe_hash)`.
14. ExportPackage freezes exact membership and one generation execution mode.
15. Database constraints complement command-interface rules; neither replaces RLS, authorization, idempotency, or external-call coordination.

## Completion gates for Issue #21

Issue #21 is complete only when:

- Sections 1–5 are reflected in the implementation and committed specification;
- all new mutation callers use the new interface and emit only versioned `generation.*` Jobs;
- the production cutover runbook through D4 is executable and rehearsed;
- legacy creation is disabled and the drain/backfill/verifier gates are green;
- PostgreSQL and SQLite migration suites pass at their respective responsibility levels;
- controlled Mock, BYOK, unavailable Sandbox, uncertainty, reconciliation, cost, QC, review, regeneration, and export evidence pass;
- OpenAPI, BFF policy, generated clients/validators, worker payload registries, and committed contracts have no drift;
- cross-tenant, role, authorization epoch, entitlement, credential, mode, object, and secret-redaction negative tests pass;
- the end-to-end domain path produces an Approved controlled-Mock AssetVersion and deterministic single-mode ZIP/Manifest without creating Experiment truth;
- repository verification is green and no supported safe in-scope implementation work remains.

M4 destructive cleanup may remain scheduled for the later observed contract release. Deprecated columns do not justify unsafe same-release deletion and do not prevent #21 acceptance when every supported read/write path already treats the new model as authoritative.

This concludes the Issue #21 design: Section 1 fixes entity identity and immutability; Section 2 owns lifecycle and recovery; Section 3 fixes HTTP/Worker interfaces; Section 4 fixes execution-mode policy and provenance; Section 5 fixes implementation order, production cutover, migration truthfulness, and automated evidence.
