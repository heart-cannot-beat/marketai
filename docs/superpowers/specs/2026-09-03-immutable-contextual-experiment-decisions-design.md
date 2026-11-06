# Issue #22 — Complete Design: Immutable Contextual Experiment Decisions

> **Generated**: 2026-09-03T13:14:41Z
> **Status**: Design candidate — not yet approved for implementation.
> **Assembly**: Concatenation of approved sections, delta, cross-section addendum, Section 5 (Implementation Readiness), and Final Acceptance Traceability.

---

<!-- source-boundary: section1-final -->
# Section 1: ExperimentVersion Immutable Inputs

## §1.0 Design overview

ExperimentRecord stays the mutable Draft→frozen→running→closed lifecycle identity with optimistic revision. One ExperimentVersion per Experiment; changing a frozen intent creates a new Experiment (CONTEXT.md).

Product identity bridge: Add nullable `product_profile_entry_id` to ProductRecord, composite FK → `library_entries`. This bridges the legacy product identity to the versioned library system. Freeze validates the chain through to exact ProductVersion and OfferVersion.

New write-once tables:

- **ExperimentVersionRecord** — immutable frozen snapshot. References exact ProductVersion and OfferVersion (composite tenant FKs to `library_versions`; subtype and content enforced by command validation, not database FK). Singular market and singular placement for v1. Audience as frozen content + hash. Budget as `Numeric(18,6)` normalized projections verified equivalent to canonical decimal strings at insert (fail-closed on mismatch). Landing-page evidence hash extracted from OfferVersion; no independent LP field. `frozen_snapshot_hash` has a normal index, not UNIQUE — two experiments may freeze identical intent.
- **ExperimentVersionCandidateRecord** — one per frozen candidate. Post-freeze identity for ad bindings, judgement, proposals, and decisions. Carries `source_kind` + nullable `source_run_item_id` (`source_evidence_json` for additional provenance). Each candidate has `candidate_hash` over full content and `concepts_hash` over its concept rows.
- **ExperimentVersionCandidateConceptRecord** — one per dimension concept per candidate (Hook, Angle, Persona, etc.). Simple FK to `dimension_concept_keys.id` with scope-aware service validation (platform keys have `workspace_id=NULL`). `duplicate_group_hash` identifies same-concept groups — not automatically a conflict.
- **ExperimentVersionConceptResolutionRecord** — one per duplicate-concept group at version level. Supports `allow_controlled_duplicate`, `exclude_source_candidate`, `remap_before_freeze`. Excluded candidates preserved here by stable source fingerprint, not as active frozen candidates. Unresolved groups block freeze.
- **DecisionProposalRecord** — immutable header. Proposed verdicts preserving inconclusive statistical output. No mutable status column; lifecycle tracked by append-only state events.
- **DecisionProposalStateEventRecord** — append-only lifecycle events for proposals. Monotonic `sequence` per proposal enables CAS-safe state transitions.
- **DecisionConfirmationRecord** — append-only. Binds proposal to idempotency key, request fingerprint, expected proposal sequence, actor, role, authorization_epoch, authorization evidence, confirmed timestamp. Rejects missing, stale, superseded, consumed, or all-inconclusive proposals. Enforces one material confirmation per proposal.

Additively evolved existing tables (`workspace_id` added):

- **ExperimentEntryRecord** — add `workspace_id`. Draft-only identity; not used on the new path after freeze.
- **ExperimentAdBindingRecord** — add `workspace_id`, `experiment_version_candidate_id`, `record_schema_version` (NULL=legacy, 2=new). New-path bindings reference frozen candidate. Replace global UNIQUE with tenant-scoped.
- **JudgementRecord** — add `workspace_id`, `experiment_version_id`, `record_schema_version`. New-path `run_judgement()` reads candidates directly, never traverses Draft entries.
- **ExperimentDecisionRecord** — add `workspace_id`, nullable `decision_schema_version` (NULL=legacy, 2=new). Legacy rows untouched, `experiment_entry_id` required. New rows: `experiment_version_id`, `experiment_version_candidate_id`, `proposal_id`, `confirmation_id`, `actor_id`, `confirmed_at`, `context_hash`. `actor_id`/`confirmed_at` are integrity projections copied from Confirmation and verified equal at insert.
- **ExperimentStateEventRecord** — add `workspace_id`.

Read projection (rebuildable, not truth):

- **ContextualVerdictProjection** — experiment-specific key: `(workspace_id, experiment_id, asset_version_id, offer_version_id, market, placement, landing_page_evidence_hash)`. Rebuilt from ExperimentDecision rows. `inconclusive` never projects. Cross-experiment "current primary winner" is a separate policy projection with explicit comparability, supersession, revocation and withdrawal rules — not "latest confirmed wins" across experiments.

Legacy (no new writes under #22):

- **VerdictDecisionRecord** — `set_verdict()` stops writing. `get_verdicts()` reads existing rows as fallback during transition.

Canonical verdict vocabulary: Winner=`win`, Learning/Learner=`learn`, Loser=`lose`; `inconclusive` is evidence status — never a material contextual verdict, never unlocks Localization or Scale.

Judge boundary: Pure `judge()` stays I/O-free and environment-free. Input contract evolves (entries keyed by frozen candidate ID, schema → `"judge-v2"`). Statistical algorithms verified by regression fixtures. Orchestration changes: `run_judgement()` appends only JudgementRecord + DecisionProposalRecord; material ExperimentDecision rows written only by `confirm_proposal()`.

---

## §1.1 Product identity bridge

`ExperimentRecord.product_id` and `AssetRecord.product_id` reference legacy ProductRecord. ProductVersion and OfferVersion are LibraryVersionRecord rows with `entry_type='product'`/`'offer'`. No FK bridge exists today.

Add nullable `product_profile_entry_id` String(64) to ProductRecord, composite FK `(workspace_id, product_profile_entry_id) → (library_entries.workspace_id, library_entries.id)`. Service validates `entry_type='product'`.

Add a tenant-scoped partial unique index: `UNIQUE(workspace_id, product_profile_entry_id) WHERE product_profile_entry_id IS NOT NULL` — two ProductRecords in the same Workspace cannot silently claim the same ProductProfile entry.

No backfill by name/content guessing. There is no trustworthy legacy association. Existing rows remain `product_profile_entry_id=NULL` until an explicit audited link is established or a deterministic migration creates a new dedicated ProductProfile identity. Freeze fails with a clear error code (`product_profile_not_linked`) while the bridge is NULL.

At freeze, `freeze_experiment()` validates:
- `ProductRecord.product_profile_entry_id IS NOT NULL`
- `ProductVersion.entry_id == ProductRecord.product_profile_entry_id` — the frozen ProductVersion must belong to the same ProductProfile as the Experiment's product
- Both `LibraryEntryRecord.entry_type='product'` and `LibraryVersionRecord.entry_type='product'`
- ProductVersion: `state='effective'`, `eligibility='eligible'`, same Workspace
- OfferVersion: `entry_type='offer'`, same state/eligibility, same Workspace
- `OfferVersion.content_json.product_version_id` equals the exact frozen ProductVersion ID
- OfferVersion market scope covers the experiment's market
- OfferVersion validity period covers the experiment schedule interval
- Landing-page evidence: `OfferVersion.content_json.landing_page_url` extracted into the canonical snapshot; canonical LP evidence hash computed. No independent landing-page field on ExperimentVersionRecord.

Generic FKs to `library_versions` cannot enforce subtype or content at the database level. Command-time validation plus frozen evidence in the canonical snapshot enforce these.

---

## §1.2 ExperimentVersionRecord

Created exactly once during `freeze_experiment()`. Never updated. One per Experiment.

Source-of-truth rule: Snapshot and normalized projections are created from one canonical object in a single transaction. Normalized columns are verified equivalent to the canonical representation at insert; mismatch fails insertion (fail-closed). There is no read-time "snapshot wins" fallback. Write-once/never-deleted subject to the controlled Workspace erasure/retention procedure.

Identity and versioning:
- `id` String(64) PK
- `workspace_id` — composite FK; `UNIQUE(workspace_id, id)`
- `experiment_id` — composite FK `(workspace_id, experiment_id) → (experiments.workspace_id, experiments.id)`; `UNIQUE(workspace_id, experiment_id)` — one version per experiment
- `canonicalization_version` String(40)

Frozen business context (integrity projections):
- `product_version_id` String(64) — composite FK `(workspace_id, product_version_id) → (library_versions.workspace_id, library_versions.id)`. Required. Command validates entry_type, entry_id match, state, eligibility (§1.1).
- `offer_version_id` String(64) — composite FK `(workspace_id, offer_version_id) → (library_versions.workspace_id, library_versions.id)`. Required. Command validates entry_type, product_version match, market scope, validity interval (§1.1).
- `market` String(10) — singular, ISO 3166-1 alpha-2. Validated against OfferVersion market scope.
- `placement` String(40) — singular for v1. Matches acceptance contract language and current judge aggregation. Multi-placement deferred: would require immutable delivery-context identifiers and per-context judge/verdict.
- `landing_page_evidence_hash` String(64) — SHA-256 of canonical LP evidence extracted from OfferVersion. No independent LP field.
- `audience_snapshot_json` JSON — frozen audience specification (evidence)
- `audience_snapshot_hash` String(64) — SHA-256 of canonical audience content

Budget and schedule (Numeric projections verified against canonical decimal strings):
- `daily_budget_amount` Numeric(18,6) — CHECK > 0
- `total_budget_cap` Numeric(18,6) — CHECK >= daily_budget_amount
- `budget_currency` String(3) — ISO 4217, CHECK length=3. Validated against Workspace budget contract and frozen cost context. Not validated against OfferVersion — `library/contracts.py` forbids price/currency inside OfferVersion.
- `schedule_start_at` DateTime(tz) — exact instant
- `schedule_end_at` DateTime(tz) — CHECK > schedule_start_at
- `schedule_timezone` String(100) — IANA timezone

Attribution and statistical contract:
- `attribution_click_window_days` Integer
- `attribution_view_window_days` Integer
- `metric_contract_hash` String(64)
- `statistical_plan_json` JSON — canonical: primary_metric, secondary_metrics (strings), alpha, bootstrap_samples, min_days
- `statistical_plan_hash` String(64)

Marketing vars schema:
- `marketing_vars_schema_version` String(40) — versioned candidate-level creative-factor allowlist (persona, angle, hook, copy variant fields). Candidate marketing vars must not redefine experiment-owned ProductVersion, OfferVersion, landing page, market, placement, or audience.

Candidate and resolution integrity:
- `candidates_hash` String(64) — SHA-256 over canonical ordered `candidate_hash` values
- `concept_resolutions_hash` String(64) — SHA-256 over canonical sorted resolution rows

Canonical snapshot:
- `frozen_snapshot_json` JSON — full canonical snapshot; commits both `candidates_hash` and `concept_resolutions_hash`
- `frozen_snapshot_hash` String(64) — SHA-256 of canonical JSON. Normal (non-UNIQUE) index. Two distinct experiments may freeze identical intent. `UNIQUE(workspace_id, experiment_id)` already enforces one version per experiment.
- `created_at` DateTime(tz)

---

## §1.3 ExperimentVersionCandidateRecord

Immutable frozen candidates, created atomically with ExperimentVersionRecord. Post-freeze identity for ad bindings, judgement, proposals, and decisions. ExperimentEntryRecord is Draft-only.

Identity:
- `id` String(64) PK; `workspace_id` composite UNIQUE
- `experiment_version_id` — composite FK
- `position` Integer — `UNIQUE(experiment_version_id, position)`
- `asset_version_id` — composite FK `(workspace_id, asset_version_id) → (asset_versions.workspace_id, asset_versions.id)`; `UNIQUE(experiment_version_id, asset_version_id)`

Execution and marketing:
- `execution_mode` String(20) — `byok|sandbox|mock`. Must be uniform across all candidates; freeze rejects mixed modes.
- `marketing_vars_json` JSON — frozen creative-factor values validated against `marketing_vars_schema_version`. Must not redefine experiment-owned context fields. Legacy offer/landing_page keys in existing MarketingVars: freeze proves equality with ExperimentVersion context and strips them from canonical form on mismatch.
- `marketing_vars_hash` String(64)

Provenance (not identity):
- `source_entry_id` String(64) — composite tenant FK `(workspace_id, source_entry_id) → (experiment_entries.workspace_id, experiment_entries.id)`. Nullable, trace only.
- `source_kind` String(40) — `generation|upload|reuse|manual`. Not all AssetVersions have a RunItem.
- `source_run_item_id` String(64) — nullable. Present when `source_kind='generation'` and a RunItem exists.
- `source_evidence_json` JSON — nullable. Additional provenance by source kind.

Integrity projections:
- `asset_version_content_sha256` String(64) — frozen integrity projection of `AssetVersionRecord.content_sha256` at freeze time. Ensures the candidate hash covers the actual asset content identity, not just the version ID.
- `source_evidence_hash` String(64) — SHA-256 of canonical `source_evidence_json`. Nullable (NULL when `source_evidence_json` is NULL).

Eligibility evidence (authoritative at freeze):
- `eligibility_evidence_json` JSON — see §1.5
- `eligibility_evidence_hash` String(64)
- `evaluated_at` DateTime(tz)

Hash integrity:
- `concepts_hash` String(64) — SHA-256 of canonical sorted concept rows for this candidate
- `candidate_hash` String(64) — SHA-256 of `(position, asset_version_id, asset_version_content_sha256, execution_mode, marketing_vars_hash, concepts_hash, eligibility_evidence_hash, source_kind, source_evidence_hash)`. All named fields exist as columns.

Write-once, no UPDATE path.

---

## §1.4 Candidate concepts and duplicate resolution

**ExperimentVersionCandidateConceptRecord** — one per dimension concept per candidate. Write-once.

- `id`, `workspace_id` (composite UNIQUE)
- `experiment_version_candidate_id` — composite FK
- `dimension_key` String(200) — e.g. "hook", "angle", "persona". `UNIQUE(experiment_version_candidate_id, dimension_key)`.
- `stable_dimension_signature` String(200) — frozen from system definition or manual assignment
- `concept_key_id` String(64) — simple FK → `dimension_concept_keys.id`. Not composite tenant FK — DimensionConceptKey allows `workspace_id=NULL` for platform-scope keys.
- `concept_key_scope` String(20) — `workspace|platform`. Service validates: workspace-scope keys must have `concept_key.workspace_id = this.workspace_id`; platform-scope must have `concept_key.workspace_id IS NULL`.
- `concept_key_name` String(300) — frozen at freeze time
- `value_json` JSON — frozen dimension value/label/prompt content
- `value_hash` String(64)
- `evidence_json` JSON — `{source_kind, run_item_id (nullable), evidence_hash}`
- `duplicate_group_hash` String(64) — SHA-256 of `(dimension_key, concept_key_id)`. Two candidates with the same hash form a duplicate-concept group. Not automatically a conflict — two ads may deliberately share a Hook while differing in Angle.
- `created_at` DateTime(tz)

**ExperimentVersionConceptResolutionRecord** — one per duplicate-concept group at version level. Write-once.

- `id`, `workspace_id` (composite UNIQUE)
- `experiment_version_id` — composite FK
- `duplicate_group_hash` String(64)
- `dimension_key` String(200), `concept_key_id` String(64) — readability
- `source_candidate_fingerprints_json` JSON — stable fingerprints of all Draft candidates in the group before resolution. Excluded Draft candidates preserved here by source fingerprint, not as active frozen candidates.
- `action` String(40) — `allow_controlled_duplicate|exclude_source_candidate|remap_before_freeze`
- `outcome_evidence_json` JSON — describes the resolution outcome: which candidates remained, which were excluded, what was remapped and to what
- `reason` Text
- `actor_id` String(100)
- `authorization_evidence_json` JSON
- `resolved_at` DateTime(tz)
- `created_at` DateTime(tz)

Each candidate's `concepts_hash` covers its concept rows. `concept_resolutions_hash` on ExperimentVersionRecord covers resolution rows. `frozen_snapshot_hash` commits both. No circular cross-references.

Unresolved duplicate groups block freeze.

---

## §1.5 Eligibility evidence

Draft creation is advisory: `create_experiment()` calls eligibility checks and warns but does not block.

Freeze is authoritative. `freeze_experiment()` rechecks every gate and persists exact evidence in `eligibility_evidence_json`:

- **Approved review**: frozen `QualityReviewRecord.id`, `review_value`, `review_created_at`, `superseded_count`, `review_set_hash`, `review_ordering`, `linearization_lock` (§2.1)
- **QC state**: relevant `AssetQCAttemptRecord` IDs, states; `AssetQCCheckResultRecord` IDs and hashes. No `failed_blocking` attempt outstanding unless validly remediated (§2.2)
- **AssetVersion identity**: `content_sha256`, provenance fields, `execution_mode`
- **Library versions**: ProductVersion and OfferVersion IDs, hashes, state, eligibility
- **Dependency/binding evidence**: applicable TruthPackage and MarketAssetBinding stale evidence. `stale_truth` is not an AssetVersion column — it is evaluated through the dependency/binding chain
- **Context match**: workspace, product, offer compatibility
- **Execution mode**: uniform canonical `byok|sandbox|mock`
- `eligibility_policy_version` String — identifies the rules applied
- `evaluated_at` on candidate record — exact freeze-time evaluation

No `eligibility_revision` field — no single epoch exists across external dependencies.

Later withdrawal or staleness never rewrites the frozen version. Post-freeze changes trigger start/run/confirmation commands to re-evaluate and create new invalidation/audit evidence (§2.5).

---

## §1.6 Hash integrity chain

No circular dependencies. Each entity computes an independent hash.

- Per concept row: `value_hash` = SHA-256 of canonical value
- Per candidate: `concepts_hash` = SHA-256 of canonical sorted concept rows. `candidate_hash` = SHA-256 of `(position, asset_version_id, asset_version_content_sha256, execution_mode, marketing_vars_hash, concepts_hash, eligibility_evidence_hash, source_kind, source_evidence_hash)`
- Overall: `frozen_snapshot_hash` = SHA-256 of full canonical snapshot JSON, which commits `candidates_hash`, `concept_resolutions_hash`, and all version-level context.

Canonical JSON represents money as decimal strings. Normalized `Numeric(18,6)` columns are verified equivalent at insertion. Mismatch = fail-closed.

---

## §1.7 DecisionProposalRecord and DecisionConfirmationRecord

**DecisionProposalRecord** — immutable header, no mutable status column.

- `id`, `workspace_id` — `UNIQUE(workspace_id, id, experiment_id, experiment_version_id, judgement_id)` — 5-column lineage anchor for Decision composite FK
- `experiment_id`, `experiment_version_id` — composite tenant FKs
- `judgement_id` — composite FK
- `eligibility_context_hash` String(64) — canonical hash of eligibility inputs per §4.2.1 formula. `UNIQUE(workspace_id, judgement_id, eligibility_context_hash)` — proposal deduplication (identical judgement + identical eligibility context = idempotent load)
- `proposed_entries_json` JSON — per candidate: `{candidate_id, verdict, metric, estimate, interval_low, interval_high, reason_codes, totals}`. Preserves inconclusive statistical output.
- `evidence_json` JSON — recheck evidence captured at proposal creation time
- `proposal_hash` String(64) — `_sha256(_canonical_bytes((eligibility_context_hash, judgement_snapshot_hash, proposed_entries_canonical)))` — 3 hash inputs that transitively commit judgement ID, algorithm version, ExperimentVersion ID, and context hash.
- `created_at` DateTime(tz)

No status column. Proposal lifecycle is tracked by append-only events.

**DecisionProposalStateEventRecord** — append-only lifecycle events.

- `id`, `workspace_id` (composite UNIQUE)
- `proposal_id` — composite FK `(workspace_id, proposal_id) → (decision_proposals.workspace_id, decision_proposals.id)`
- `sequence` Integer — monotonic per proposal. `UNIQUE(workspace_id, proposal_id, sequence)`.
- `event_type` String(32) — CHECK IN (`created`, `consumed`, `invalidated`, `superseded`)
- `actor_id` String(100) — nullable (system for `created`)
- `reason` Text — nullable
- `evidence_json` JSON — nullable (e.g. superseding proposal ID)
- `created_at` DateTime(tz)

Every proposal is born with a `sequence=1, event_type='created'` row inserted atomically with the header. Current status is the latest event by sequence — rebuildable. A CAS status projection column may be added for query speed but is not truth.

**DecisionConfirmationRecord** — append-only. Revised fields:

- `id`, `workspace_id` (composite UNIQUE)
- `proposal_id` — composite FK. `UNIQUE(workspace_id, proposal_id)` — one material confirmation per proposal.
- `proposal_hash` String(64) — must match proposal at confirmation time
- `expected_proposal_sequence` Integer — must equal the proposal's current (latest) sequence; confirmation fails if a supersession event arrived between read and confirm.
- `idempotency_key_hash` String(64) — `UNIQUE(workspace_id, idempotency_key_hash)`. On retry, request fingerprint must match the existing row; mismatch rejects.
- `request_fingerprint_hash` String(64)
- `actor_id` String(100)
- `role` String(20) — `owner|member`
- `authorization_epoch` Integer
- `authorization_evidence_json` JSON
- `confirmed_at` DateTime(tz)
- `created_at` DateTime(tz)

Confirmation transaction (single atomic unit):
1. Verify `proposal_hash` matches proposal header
2. Verify proposal's latest event sequence equals `expected_proposal_sequence` and event is `created` (pending state)
3. Insert DecisionConfirmationRecord (`UNIQUE` on `proposal_id` prevents double-confirm)
4. Append `consumed` event to DecisionProposalStateEventRecord
5. Insert ExperimentDecisionRecord rows only for entries with `win|learn|lose` verdicts

Mixed proposals: A proposal containing both sufficient (`win|learn|lose`) and inconclusive entries is confirmable. Material Decisions are emitted only for the sufficient entries. An all-inconclusive proposal is not confirmable — confirmation rejects with a clear error code.

Confirmation also rejects: missing proposal, stale `proposal_hash`, superseded (latest event is `superseded`), already-consumed (latest event is `consumed`), proposal from different Workspace or ExperimentVersion.

---

## §1.8 ExperimentDecisionRecord evolution

Add nullable `decision_schema_version` Integer; NULL = legacy. New rows require 2. Existing rows left completely untouched — no backfill.

Legacy row shape (`decision_schema_version IS NULL`):
- `experiment_entry_id` NOT NULL (existing constraint)
- All new columns NULL
- Partial unique: `UNIQUE(judgement_id, experiment_entry_id) WHERE decision_schema_version IS NULL`

New-path row shape (`decision_schema_version = 2`):
- `experiment_entry_id` NULL. Provenance already captured by `ExperimentVersionCandidateRecord.source_entry_id`.
- `experiment_version_id` NOT NULL — composite FK
- `experiment_version_candidate_id` NOT NULL — composite FK
- `proposal_id` NOT NULL — composite FK
- `confirmation_id` NOT NULL — composite FK
- `actor_id` NOT NULL String(100) — integrity projection from Confirmation, verified equal at insert
- `confirmed_at` NOT NULL DateTime(tz) — integrity projection from Confirmation, verified equal
- `context_hash` NOT NULL String(64) — `_sha256(_canonical_bytes((experiment_id, offer_version_id, market, placement, landing_page_evidence_hash)))` — 5 inputs per §4 formula
- Partial unique: `UNIQUE(workspace_id, confirmation_id, experiment_version_candidate_id) WHERE decision_schema_version = 2`

Bidirectional CHECK:
```
CHECK (
  CASE
    WHEN decision_schema_version IS NULL THEN
      experiment_entry_id IS NOT NULL
      AND experiment_version_id IS NULL
      AND experiment_version_candidate_id IS NULL
      AND proposal_id IS NULL
      AND confirmation_id IS NULL
      AND actor_id IS NULL
      AND confirmed_at IS NULL
      AND context_hash IS NULL
    WHEN decision_schema_version = 2 THEN
      experiment_entry_id IS NULL
      AND experiment_version_id IS NOT NULL
      AND experiment_version_candidate_id IS NOT NULL
      AND proposal_id IS NOT NULL
      AND confirmation_id IS NOT NULL
      AND actor_id IS NOT NULL
      AND confirmed_at IS NOT NULL
      AND context_hash IS NOT NULL
    ELSE FALSE
  END
)
```

`experiment_id` and `judgement_id` remain NOT NULL on all rows (both legacy and new). For new-path rows, service validates that these equal the referenced Proposal/Confirmation lineage (same experiment, same judgement chain). `actor_id` and `confirmed_at` are integrity projections verified equal to the Confirmation record at insert.

---

## §1.9 Additive tenant migration

Five existing tables lack `workspace_id`: `experiment_entries`, `experiment_ad_bindings`, `judgements`, `experiment_decisions`, `experiment_state_events`.

Backfill is deterministic: each row's `experiment_id` (or `experiment_entry_id → experiment_id`) reaches `ExperimentRecord.workspace_id` (NOT NULL). Per table: ADD COLUMN nullable, UPDATE from parent, ALTER SET NOT NULL, ADD composite FK, ADD/REPLACE UNIQUE constraints.

**ExperimentEntryRecord**: add `workspace_id`, composite FK for experiment and asset_version, `UNIQUE(workspace_id, id)`. Role unchanged: Draft-only.

**ExperimentAdBindingRecord**:
- Add `workspace_id` (backfilled through entry → experiment)
- Add `experiment_version_candidate_id` nullable
- Add `record_schema_version` nullable Integer (not `schema_version` — avoids confusion with judge input snapshot schema version)
- Replace global `UNIQUE(ad_account_id, ad_id)` with `UNIQUE(workspace_id, ad_account_id, ad_id)`
- Partial unique — legacy: `UNIQUE(experiment_entry_id) WHERE record_schema_version IS NULL` (one binding per Draft entry)
- Partial unique — new: `UNIQUE(experiment_version_candidate_id) WHERE record_schema_version = 2` (one binding per frozen candidate)
- Bidirectional CHECK:
```
CASE
  WHEN record_schema_version IS NULL THEN
    experiment_entry_id IS NOT NULL
    AND experiment_version_candidate_id IS NULL
  WHEN record_schema_version = 2 THEN
    experiment_version_candidate_id IS NOT NULL
  ELSE FALSE
END
```
(New-path `experiment_entry_id` may be NULL or provenance-only per the same rule as Decision.)

**JudgementRecord**:
- Add `workspace_id`, `experiment_version_id` nullable
- Add `record_schema_version` nullable Integer (not `schema_version`)
- Composite FK for experiment; composite FK for experiment_version on new path
- Replace `UNIQUE(experiment_id, snapshot_hash, algorithm_version)` with tenant-scoped `UNIQUE(workspace_id, experiment_id, snapshot_hash, algorithm_version)`
- `UNIQUE(workspace_id, id)`
- Bidirectional CHECK:
```
CASE
  WHEN record_schema_version IS NULL THEN
    experiment_version_id IS NULL
  WHEN record_schema_version = 2 THEN
    experiment_version_id IS NOT NULL
  ELSE FALSE
END
```
- New-path `run_judgement()` reads ExperimentVersionCandidate rows directly, never traverses Draft entries

**ExperimentStateEventRecord**: add `workspace_id`, composite FK, `UNIQUE(workspace_id, id)`.

---

## §1.10 ContextualVerdictProjection

Rebuildable read-side projection. Historical truth is in ExperimentDecisionRecord.

Experiment-specific key: `(workspace_id, experiment_id, asset_version_id, offer_version_id, market, placement, landing_page_evidence_hash)`

Projected fields: `verdict` (`win|learn|lose` — never `inconclusive`), `experiment_version_id`, `decision_id`, `metric`, `estimate`, `updated_at`.

Rebuild rule: per unique context tuple, take the most recent confirmed non-legacy ExperimentDecisionRecord with a material verdict, ordered by `(confirmed_at DESC, decision_id DESC)` as a stable total order. Timestamp ties are broken deterministically by `decision_id`.

Exclude Decisions whose Confirmation has been invalidated by explicit revocation or withdrawal events, when such events exist.

Cross-experiment "current primary winner" is a separate policy projection with explicit comparability (same product, offer context), supersession, revocation, and withdrawal rules. Not defined by Issue #22.

---

## §1.11 VerdictDecisionRecord transition

Currently written by `store.set_verdict()` (source="legacy") and JSON importer. Under #22: `set_verdict()` stops writing. `get_verdicts()` reads existing rows as fallback during transition. JSON importer classifies imported rows as legacy. No rows deleted or modified.

---

## §1.12 Judge boundary and orchestration

Pure `judge()` stays I/O-free, clock-free, environment-free. Input contract evolves: entries keyed by `ExperimentVersionCandidate.id`, schema → `"judge-v2"`. `"judge-v1"` + fixtures remain valid for legacy snapshots. Statistical algorithms verified by regression fixtures.

Orchestration change:
- Current: `run_judgement()` → `judge()` → JudgementRecord → ExperimentDecisionRecord (direct, traverses Draft entries)
- New: `run_judgement()` reads frozen candidates + new-path ad bindings → `judge()` → JudgementRecord (with `experiment_version_id`) → DecisionProposalRecord. Separate `confirm_proposal()` → DecisionConfirmationRecord → ExperimentDecisionRecord (only `win|learn|lose`; `inconclusive` cannot become material).

---

## §1.13 Coverage map

| Delta | Where applied |
|---|---|
| 1. Composite tenant FKs for ProductVersion/OfferVersion | §1.2 (composite FKs replace simple FKs). §1.1 (no backfill, partial unique on bridge, freeze fails while NULL, verify entry_id match and entry_type on both sides) |
| 2. Remove candidate `conflict_resolution_json`; complete provenance hashes | §1.3 (field removed; `asset_version_content_sha256`, `source_evidence_hash` added; `source_entry_id` now composite FK; `candidate_hash` input updated). §1.4 (`kept_candidate_evidence_json` → `outcome_evidence_json`) |
| 3. Immutable Proposal header + state events + race guards | §1.7 (status column removed; DecisionProposalStateEventRecord added; `expected_proposal_sequence` on Confirmation; `UNIQUE(workspace_id, proposal_id)` and `UNIQUE(workspace_id, idempotency_key_hash)` race guards; mixed-proposal rule; `proposal_hash` content defined) |
| 4. Bidirectional legacy/new CHECKs | §1.8 (CASE/WHEN CHECK enforcing both NULL and 2 branches, ELSE FALSE; `experiment_entry_id` NULL in new path). §1.9 (`record_schema_version` naming on AdBinding/Judgement; bidirectional CHECKs; AdBinding partial uniques per-entry legacy + per-candidate new) |
| 5. Deterministic projection ordering | §1.10 (`confirmed_at DESC, decision_id DESC`; exclude invalidated decisions) |
| Deferred to Section 2 | Effective review precedence and QC remediation semantics — stated explicitly in §2.1/§2.2 |

---

*Section 1 reconstructed from approved sources. Yields to approved Sections 2–4 on any conflict.*


<!-- source-boundary: section2-final -->
# Section 2: Eligibility Matrix and Deterministic Effective-Review / QC-Remediation Semantics

## §2.1 Effective review precedence

### Current behavior (bug)

`is_ready_for_experiment()` queries `review_value == 'approved'` with `scalar_one_or_none()`. This is doubly unsafe: if zero approved rows exist it returns `None` (not approved — correct), but if multiple approved rows exist it raises `MultipleResultsFound`. It also considers all `QualityReviewRecord` rows regardless of `legacy_unscoped` or `asset_version_id` exactness.

### New semantics

The ordered review set for a given `(workspace_id, asset_version_id)` includes every `QualityReviewRecord` row satisfying:

- `asset_version_id` equals the exact version (not NULL)
- `legacy_unscoped = false`

No filter on `review_value` — NULL and unknown values remain in the set. The latest row by total order `(created_at DESC, id DESC)` is authoritative. Its `review_value` determines the gate outcome:

| Effective `review_value` | Gate outcome | Diagnostic code |
|---|---|---|
| `approved` | `pass` | — |
| `rejected` | `fail` | `review_not_approved` |
| `needs_changes` | `fail` | `review_not_approved` |
| `NULL` | `fail` | `review_unknown` |
| Any other value | `fail` | `review_unknown` |
| No qualifying rows | `fail` | `review_not_approved` |

A NULL or unrecognized `review_value` on the latest row fails closed — it does not get skipped to let an older `approved` row take effect.

A later rejection supersedes an earlier approval. A later re-approval re-approves. The `quality_feedback` regeneration intent produces a different AssetVersion — it is not a re-review of the original.

### Canonical review-set hash

The hash covers deterministic tuples `(id, review_value, created_at)` for every qualifying row, sorted by the same total order. This commits not just which reviews exist but what each one said and when.

### Evidence

```
effective_review_id           -- the authoritative QualityReviewRecord.id
review_value                  -- its review_value (including NULL if applicable)
review_created_at             -- its created_at
superseded_count              -- number of earlier qualifying rows
review_set_hash               -- SHA-256 of canonical (id, review_value, created_at) tuples
review_ordering               -- "created_at DESC, id DESC"
linearization_lock            -- "asset_version_for_update"
```

No unbounded `superseded_review_ids` list. The `review_set_hash` commits the full set; `superseded_count` is the diagnostic summary.

### Linearization contract

Eligibility evaluation and review/QC writes must agree on which writes are visible. Every command that writes a `QualityReviewRecord` or `AssetQCAttemptRecord`, and every eligibility evaluation (freeze/start/judge/confirm), acquires `SELECT ... FOR UPDATE` on the affected `AssetVersionRecord` rows in sorted `id` order before reading reviews or QC attempts. `freeze_experiment()` additionally locks the exact `LibraryVersionRecord` rows for the frozen ProductVersion and OfferVersion, and any local truth-dependency rows.

Serialization requires that every command that mutates the locked rows also acquires the same locks. The full concurrency contract:

| Command | Rows locked FOR UPDATE (in global class order) |
|---|---|
| `create_review()` | AssetVersionRecord (the reviewed AV) |
| QC attempt creation/transition | AssetVersionRecord (the QC'd AV) |
| LibraryVersion state/eligibility mutation (withdraw, block, supersede) | LibraryVersionRecord (the mutated version) |
| Draft entry mutation (add/remove/update entry) | Experiment aggregate (ExperimentRecord CAS) |
| `freeze_experiment()` | Experiment aggregate → AssetVersions → LibraryVersions → truth-dependency rows |
| `bind_candidate()` | Experiment aggregate |
| `start_experiment()` | Experiment aggregate → AssetVersions → LibraryVersions → truth-dependency rows → eligibility rows |
| `run_judgement()` | Experiment aggregate → AssetVersions → LibraryVersions → truth-dependency rows → eligibility rows → Proposal aggregate |
| `confirm_proposal()` | Experiment aggregate → AssetVersions → LibraryVersions → truth-dependency rows → eligibility rows → Proposal aggregate |
| `close_experiment()` (invalidated) | Experiment aggregate → eligibility rows |
| Review/QC invalidation propagation | AssetVersion → eligibility rows → Proposal aggregate |
| LibraryVersion invalidation propagation | LibraryVersion → eligibility rows → Proposal aggregate |
| Truth invalidation propagation | truth-dependency rows → eligibility rows → Proposal aggregate |

Draft entry add/remove/update commands increment `ExperimentRecord.revision`. The concept-resolution plan is command-scoped input to `freeze_experiment()`, bound to the request fingerprint/idempotency key and `expected_revision` — there is no persisted draft resolution mutation that increments revision independently. The final snapshot INSERT and state CAS (`UPDATE ... WHERE revision = expected`) execute in the same transaction, so a concurrent draft edit cannot be silently omitted.

Evidence evaluation and the immutable snapshot INSERT happen inside the same transaction, after locks. A review or QC write committed after the lock/snapshot linearization point is a post-freeze invalidation event (§2.5). A write committed before the lock must be included in the evaluation.

### Global lock order (deadlock avoidance)

All lock-acquiring commands follow one fixed class order:

1. Experiment aggregate: `ExperimentRecord`, then applicable Entry/Version/Candidate/Binding rows in deterministic ID order
2. AssetVersionRecords ascending by id
3. LibraryVersionRecords ascending by id
4. Truth-dependency rows ascending by id
5. Eligibility event/projection rows in deterministic version/candidate order
6. Proposal aggregate: `DecisionProposalRecord`, then its latest/state-event rows in deterministic proposal/sequence order

Within each class, rows are sorted ascending by id. Commands start at the earliest class they need and never acquire an earlier class later. A command that locks only one class (e.g. `create_review()` locks one AssetVersion, or a LibraryVersion mutation locks one LibraryVersionRecord) is consistent because it acquires only a single class and cannot form a cross-class cycle.

An invalidation propagator (review/QC, LibraryVersion, truth) starts at class 2, 3, or 4 and may acquire classes 5–6 but must not acquire class 1 (Experiment aggregate). This prevents the reverse edge needed for a deadlock with experiment-scoped commands that start at class 1.

### SQLite / test equivalent

SQLite uses database-level locking. `BEGIN IMMEDIATE` serializes the evaluator transaction against concurrent writes — any concurrent writer blocks until the transaction commits. Deterministic test fixtures use explicit timestamps and IDs so ordering is reproducible without relying on wall-clock.

### `create_review()` update

`create_review()` must stop calling `_has_blocking_qc_failure()` (the old "any historical failed_blocking" helper) and instead call the new effective-QC evaluator (§2.2) under the same AssetVersion `FOR UPDATE` lock. Otherwise a remediated AssetVersion (a later `passed` attempt cleared the block) can never be re-approved because the old helper still sees the historical `failed_blocking` row.

---

## §2.2 QC remediation

### Attempt sequencing

`AssetQCAttemptRecord` currently has mutable `state`/`revision` and no attempt sequence. `created_at` alone is unsafe when attempts overlap or finish out of order.

Add/evolve on `AssetQCAttemptRecord`:

- `attempt_sequence` Integer — monotonic per `(workspace_id, asset_version_id)`, UNIQUE(workspace_id, asset_version_id, attempt_sequence). Allocated under the AssetVersion `FOR UPDATE` lock (§2.1) as `MAX(attempt_sequence) + 1` or `1` for the first attempt.
- `terminal_at` DateTime(tz) — set on transition to a terminal state (`passed`, `failed_blocking`, `failed_nonblocking`, `uncertain`). NULL while `pending`/`running`.
- Partial unique: at most one active (`pending`/`running`) attempt per AssetVersion — `UNIQUE(workspace_id, asset_version_id) WHERE state IN ('pending', 'running')` (PostgreSQL partial unique; service-level enforcement under lock for SQLite).
- `remediates_attempt_id` String(64) nullable — tenant-safe self-reference to an earlier attempt on the same exact AssetVersion. Composite FK `(workspace_id, remediates_attempt_id) -> (asset_qc_attempts.workspace_id, asset_qc_attempts.id)` where possible. Service invariants enforced at insertion: same `workspace_id`, same `asset_version_id`, referenced attempt's `state = 'failed_blocking'`, referenced `attempt_sequence` is strictly lower than this attempt's sequence, and no cycle (the referenced attempt's own `remediates_attempt_id` does not transitively reference this attempt).
- **Attempt cap:** `max_qc_attempts_per_asset_version` policy limit (e.g. 20), enforced at attempt creation under the AssetVersion lock. Exceeding the cap rejects the attempt with `qc_attempt_limit_exceeded`. This bounds the snapshot and evidence size for any single AssetVersion.

### Effective QC evaluation

Under the AssetVersion `FOR UPDATE` lock:

1. If an active (`pending`/`running`) attempt exists, status is `in_progress` and freeze blocks with `qc_in_progress`.
2. Otherwise, determine the effective status from the terminal attempt history:
   a. Take the terminal attempt with the highest `attempt_sequence`.
   b. If it is `passed`, check whether any earlier `failed_blocking` attempt remains unresolved (see remediation rules below).
   c. If no unresolved block exists, status is `clear` and the gate returns `pass`.
   d. If an unresolved block exists, status is `blocked` and the gate returns `fail` with `qc_remediation_invalid`.

| Highest-sequence terminal state | Earlier unresolved block? | Effective status | Gate result |
|---|---|---|---|
| `passed` | No | Clear | `pass` |
| `passed` | Yes (remediation link missing, invalid, or policy not covered) | Blocked | `fail` (`qc_remediation_invalid`) |
| `failed_blocking` | N/A (it IS the block) | Blocked | `fail` (`qc_blocked`) |
| `failed_nonblocking` | No | Warning | `warn` |
| `failed_nonblocking` | Yes (unresolved earlier block) | Blocked | `fail` (`qc_blocked`) |
| `uncertain` | No | Warning | `warn` |
| `uncertain` | Yes | Blocked | `fail` (`qc_blocked`) |
| No terminal attempts | N/A | See QC-expected rule below | `pass` or `warn` |

### Remediation rules

A later `passed` attempt clears an earlier `failed_blocking` attempt only when ALL of the following hold:

- `remediates_attempt_id` on the passing attempt explicitly identifies the blocking attempt's ID.
- The passing attempt targets the same exact `(workspace_id, asset_version_id)` (same immutable content). Enforced by the composite FK and service invariants.
- `input_hash` on the passing attempt equals the blocking attempt's `input_hash` (same QC input).
- Policy coverage is verified: `covers(passing.qc_policy_version, passing.rule_set_version, blocked.qc_policy_version, blocked.rule_set_version)` returns a `QCCoverageDecision` with `covers=True` (see policy relation below). The decision's `registry_version` and `proof_hash` are frozen into evidence.
- If an unresolved block exists, the passing attempt must link to the currently effective unresolved block (the earliest `failed_blocking` not yet remediated by any earlier pass). A pass that links to an already-remediated block does not count.

A `passed` attempt without a valid `remediates_attempt_id`, with a non-matching `input_hash`, or without policy coverage does NOT clear the block. The version remains blocked with code `qc_remediation_invalid`. A `passed` row never becomes `clear` merely because its state says `passed`.

### Policy coverage relation

Policy coverage is determined by a `QCPolicyRegistry` read port:

```python
@dataclass(frozen=True)
class QCCoverageDecision:
    covers: bool
    registry_version: str
    proof_hash: str       # SHA-256 of canonical (candidate, blocked, relation, registry_version)

class QCPolicyRegistry(Protocol):
    def covers(self, candidate_policy: str, candidate_rules: str,
               blocked_policy: str, blocked_rules: str) -> QCCoverageDecision: ...
```

The `covers` relation is reflexive and transitive:

- Exact equality of `(policy_version, rule_set_version)` returns `QCCoverageDecision(covers=True, ...)` — the same policy covers itself. Equality still produces a versioned proof with `registry_version` and `proof_hash`.
- An explicitly registered strict supersession also returns `covers=True` — a newer policy version that the registry says covers all rules of the older version.
- Unknown or unrelated pairs return `covers=False` (fail-closed), with the same `registry_version` and a proof hash recording the negative decision.

If the design also names a `strictly_supersedes` subrelation, that subrelation is irreflexive (a policy does not strictly supersede itself). The `covers` relation is the one used for remediation checks and is reflexive.

The production implementation reads from an immutable policy version registry (a table or configuration). Tests use an in-memory adapter. The `registry_version` and `proof_hash` from the `QCCoverageDecision` are frozen into the eligibility evidence so the decision can be audited against the registry state that produced it.

### Check-result evidence hash

`AssetQCCheckResultRecord` currently has no `evidence_hash` column. Add `evidence_hash` String(64) at insertion — SHA-256 of canonical `(rule_id, rule_version, check_type, severity, outcome, canonical_evidence_json)`. All six fields are committed in the hash. Freeze evidence references check result IDs and their `evidence_hash` values, bounded to the latest effective attempt's results only.

### QC-expected rule

Whether QC is expected for an AssetVersion is determined by `origin_kind` and `execution_mode`:

- `origin_kind = 'generation'` with `execution_mode` in `{'byok', 'sandbox', 'mock'}` — QC expected.
- `origin_kind` in `{'upload', 'reuse', 'legacy'}` — QC not expected.

When QC is not expected and no attempts exist, the QC gate returns `pass`. When QC is expected but no attempts exist, the QC gate returns `warn` — advisory, not blocking. The mandatory gate remains the human review (Gate 1).

This rule applies only to the QC gate. It does not affect the stale-truth gate (§2.4), which has its own independent determination based on a versioned eligibility policy, not on origin/mode.

### Evidence

```
latest_effective_attempt_id       -- highest attempt_sequence terminal attempt
latest_effective_state            -- its terminal state
latest_effective_sequence         -- its attempt_sequence
active_attempt_id                 -- if in-progress; else null
unresolved_block_count            -- count of unresolved blocks
unresolved_block_set_hash         -- canonical hash of unresolved blocking attempt IDs
remediation_chain                 -- [{blocked_id, remediated_by_id, input_hash_match,
                                       coverage_decision: {covers, registry_version, proof_hash}}]
                                     only causal links for the effective result
qc_expected                       -- boolean (origin_kind + execution_mode rule)
qc_policy_version                 -- from latest effective attempt
check_result_ids                  -- from latest effective attempt only
check_result_hashes               -- their evidence_hash values
total_attempts                    -- count (bounded by max_qc_attempts_per_asset_version)
attempt_cap                       -- the policy cap at evaluation time
linearization_lock                -- "asset_version_for_update"
```

Evidence is bounded by the attempt cap. It includes: the latest effective attempt's check results, the count and canonical hash of unresolved blocks (not an unbounded ID list), and only the causal remediation links needed to prove the effective status.

---

## §2.3 Full eligibility matrix

### Tri-state gate outcomes

Each gate produces `pass`, `warn`, or `fail`:

- `pass` — gate satisfied, no issues.
- `warn` — gate satisfied but with advisory concerns (e.g. `failed_nonblocking` QC, missing non-required QC).
- `fail` — gate not satisfied, blocks freeze.

Freeze requires all gates to be `pass` or `warn`. Any `fail` blocks the entire freeze.

### Primary failure code precedence

Primary failure code precedence is defined independently of display gate number. Per candidate, the evaluator runs all 15 candidate-level gates, then selects the primary failure code using this precedence (highest priority first):

1. `version_withdrawn` (gate 14) — most specific, immediately actionable.
2. `product_profile_not_linked` (gate 6) — structural prerequisite.
3. `product_version_ineligible` / `offer_version_ineligible` (gates 7, 8) — state/eligibility.
4. `offer_product_mismatch` / `market_scope_mismatch` / `validity_expired` (gates 9, 10, 11) — content mismatches.
5. `asset_version_invalid` / `product_mismatch` (gates 4, 5) — identity errors.
6. `review_not_approved` / `review_unknown` (gate 1) — review status.
7. `qc_blocked` / `qc_remediation_invalid` / `qc_in_progress` (gates 2, 3) — QC status.
8. `stale_truth` / `truth_dependency_missing` / `stale_truth_unevaluable` (gate 13) — truth dependency.
9. `execution_mode_mixed` (gate 12) — cross-candidate.
10. `concept_unresolved` (gate 15) — resolution.

All gate outcomes remain in the full diagnostic response for every candidate. The primary code is what the caller sees first; the full list supports debugging.

### Gate matrix

15 candidate-level gates plus 1 version-level gate.

| # | Gate | Check | Failure codes | Outcomes |
|---|---|---|---|---|
| 1 | Review | Effective review is `approved` (§2.1); NULL/unknown fail closed | `review_not_approved`, `review_unknown` | pass / fail |
| 2 | QC | No unresolved blocking QC (§2.2); remediation validated via `covers` | `qc_blocked`, `qc_remediation_invalid` | pass / warn / fail |
| 3 | QC completeness | No active `pending`/`running` attempt | `qc_in_progress` | pass / fail |
| 4 | AV identity | AV exists, same Workspace, `content_sha256` present | `asset_version_invalid` | pass / fail |
| 5 | Product ownership | AV's Asset belongs to Experiment's Product | `product_mismatch` | pass / fail |
| 6 | Product bridge | `ProductRecord.product_profile_entry_id IS NOT NULL` | `product_profile_not_linked` | pass / fail |
| 7 | ProductVersion | `state='effective'`, `eligibility='eligible'`, `entry_type='product'`, `entry_id == product_profile_entry_id` | `product_version_ineligible` | pass / fail |
| 8 | OfferVersion | `state='effective'`, `eligibility='eligible'`, `entry_type='offer'` | `offer_version_ineligible` | pass / fail |
| 9 | Offer to Product | `content_json.product_version_id == frozen_product_version_id` | `offer_product_mismatch` | pass / fail |
| 10 | Market scope | OfferVersion market scope covers experiment `market` | `market_scope_mismatch` | pass / fail |
| 11 | Validity | OfferVersion validity covers schedule interval | `validity_expired` | pass / fail |
| 12 | Execution mode | All candidates share same `byok` / `sandbox` / `mock` | `execution_mode_mixed` | pass / fail |
| 13 | Stale-truth | Fail-closed evaluation via `StaleTruthEvaluator` (§2.4) | `stale_truth`, `truth_dependency_missing`, `stale_truth_unevaluable` | pass / fail |
| 14 | Withdrawal | Neither ProductVersion nor OfferVersion `state='withdrawn'` | `version_withdrawn` | pass / fail |
| 15 | Concept resolution | All duplicate groups involving this candidate resolved | `concept_unresolved` | pass / fail |
| 16 | Candidate count (version-level) | 2 ≤ surviving candidates ≤ 100 after resolution | `insufficient_candidates_after_resolution`, `candidate_limit_exceeded` | pass / fail |

Freeze requires all gates `pass` or `warn`. Any `fail` blocks the entire freeze. Gates 7–11 and 14 share the same LibraryVersionRecord source and are evaluated atomically. Gate 13 is strictly fail-closed (no `warn` outcome). Gate 16 is evaluated at version level after all candidate-level gates.

### Advisory subset (draft creation)

Gates 1, 2, 4, 5 only. Uses the same effective-review and effective-QC evaluators. Failures returned as warnings in the `create_experiment()` response. The experiment is created in `draft` state regardless.

### Evidence per candidate

The snapshot stores the effective bounded QC result set and the causal remediation proof, plus canonical hashes and counts for historical review and QC sets. Full historical rows remain queryable at their append-only source tables and are not duplicated into the snapshot.

```
review:
  effective_review_id, review_value, review_created_at
  superseded_count
  review_set_hash               -- canonical (id, review_value, created_at) tuples

qc:
  latest_effective_attempt_id, state, sequence
  check_result_ids, check_result_hashes   -- latest effective attempt only
  unresolved_block_count, unresolved_block_set_hash
  remediation_chain                       -- causal links only, with coverage_decision
  qc_expected, qc_policy_version, total_attempts, attempt_cap

asset_version:
  id, content_sha256, origin_kind, execution_mode

product_version:
  id, entry_id, entry_type, state, eligibility, content_hash

offer_version:
  id, entry_id, entry_type, state, eligibility, content_hash
  product_version_id_match, market_scope_covers, validity_covers_schedule

stale_truth:
  status, truth_package_id, dependency_chain
  truth_dependency_policy_version, dependency_version_token
  truth_dependency_expected, truth_dependency_evidence_hash

gate_results:
  [{gate, code, outcome, primary}]   -- all 15 candidate gates; primary=true on first fail
```

---

## §2.4 Stale-truth evaluation

### Current state

`AssetVersionRecord.truth_package_id` is a nullable opaque String(64) column — not an enforced FK. No `ForeignKeyConstraint` to a `truth_packages` table exists (that table has not been created).

### No origin/mode shortcut

`execution_mode='mock'` does not prove truth independence — mock is provider/cost provenance, not semantic claim provenance. Mock-generated content can contain the same product/offer claims as BYOK or sandbox content. `origin_kind='upload'` similarly does not prove absence of truth dependency — uploaded content can contain market-sensitive claims. Neither `execution_mode` nor `origin_kind` is used to infer truth dependency status.

### Truth-dependency columns on AssetVersionRecord

Add four write-once columns to `AssetVersionRecord`:

- `truth_dependency_expected` Boolean nullable — write-once. Set at creation (by generation worker, upload handler) or by migration backfill. NULL means the row predates the column and has not been backfilled.
- `truth_dependency_policy_version` String(40) nullable — the versioned eligibility policy that produced the determination.
- `truth_dependency_evidence_json` JSON nullable — provenance/policy evidence supporting the determination.
- `truth_dependency_evidence_hash` String(64) nullable — SHA-256 of canonical evidence tuple.

Invariants (CHECK constraints):

- `truth_dependency_expected = false` requires `truth_package_id IS NULL` — claiming no truth dependency while holding a truth reference is contradictory.
- `truth_package_id IS NOT NULL` requires `truth_dependency_expected = true` — a truth reference implies dependency.
- If `truth_dependency_expected IS NOT NULL`, then `truth_dependency_policy_version IS NOT NULL` and `truth_dependency_evidence_hash IS NOT NULL` — the evidence fields are required whenever the determination is made.

This schema delta will be carried into the eventual Section 1 / Section 5 migration consolidation.

### Not-applicable determination

Gate 13 returns `pass` (`not_applicable`) only when the AssetVersion's persisted `truth_dependency_expected = false` with a valid `truth_dependency_policy_version` and `truth_dependency_evidence_hash`. The determination comes from these write-once columns, not from a runtime inference.

### Gate 13 outcomes (fail-closed)

| Condition | Result | Code |
|---|---|---|
| `truth_dependency_expected=false` with valid policy version and evidence hash (persisted columns) | `pass` | `not_applicable` |
| `truth_dependency_expected IS NULL` (column not yet backfilled) | `fail` | `truth_dependency_missing` |
| `truth_package_id IS NULL` and no persisted evidence declares independence | `fail` | `truth_dependency_missing` |
| `truth_package_id` present, dependency chain fully evaluable, chain is current | `pass` | `clear` |
| `truth_package_id` present, dependency chain fully evaluable, chain is stale | `fail` | `stale_truth` |
| `truth_package_id` present but chain cannot be evaluated (TruthPackage/MarketAssetBinding read model absent) | `fail` | `stale_truth_unevaluable` |

No `warn` outcome for this gate. Gate 13 is strictly pass or fail.

### Grandfathered migration policy

The `v1-bootstrap` truth exemption is a one-time grandfathering decision for rows that already exist at migration time. It is not a runtime default and must not become a fallback for any future row with `truth_package_id IS NULL`.

The migration backfills the write-once truth-dependency columns on every existing `AssetVersionRecord`, split by current `truth_package_id`:

**Existing rows with `truth_package_id IS NULL`:**
- `truth_dependency_expected = false`
- `truth_dependency_policy_version = 'v1-bootstrap'`
- `truth_dependency_evidence_json = {"migration_reason": "grandfathered_pre_truth_system", "original_truth_package_id": null}`
- `truth_dependency_evidence_hash` — SHA-256 of the canonical evidence tuple

These rows are classified `not_applicable` at Gate 13 and can freeze without entering the `StaleTruthEvaluator`.

**Existing rows with `truth_package_id IS NOT NULL`:**
- `truth_dependency_expected = true`
- `truth_dependency_policy_version = 'v1-bootstrap'`
- `truth_dependency_evidence_json = {"migration_reason": "grandfathered_pre_truth_system", "original_truth_package_id": "<value>"}`
- `truth_dependency_evidence_hash` — SHA-256 of the canonical evidence tuple

These rows must enter the `StaleTruthEvaluator`. While the TruthPackage/MarketAssetBinding read model is absent, the evaluator fails closed with `stale_truth_unevaluable` and they cannot freeze. This is consistent: `truth_dependency_expected=true` means the dependency must be evaluated.

**New rows (created after migration):** The creation-time code path (generation worker, upload handler) is responsible for persisting explicit truth-dependency evidence via the write-once columns. `truth_dependency_expected` must be set based on the creation recipe/provenance and the active truth-dependency policy. Absence of evidence (`truth_dependency_expected IS NULL`) on a post-migration row fails closed with `truth_dependency_missing`.

Freeze copies the persisted determination and its evidence hash into the candidate eligibility snapshot. It does not synthesize an exemption merely because the current row has a NULL `truth_package_id`.

The `v1-bootstrap` policy version is committed into the eligibility evidence so future policy versions can audit what was exempted and tighten the rule.

### StaleTruthEvaluator consistency

The `StaleTruthEvaluator` is a protocol (abstract interface):

```python
class StaleTruthEvaluator(Protocol):
    def evaluate(self, workspace_id: str, truth_package_id: str) -> StaleTruthResult: ...
```

`StaleTruthResult` carries `status` (`clear` or `stale`), `dependency_chain`, `policy_version`, and `dependency_version_token` — an immutable evidence token (hash or version identifier) from a transactionally consistent read of the truth graph.

Freeze commits the `dependency_version_token` into the eligibility evidence. An upstream truth change committed after the linearization point (after the evaluator's consistent read under locks) is a post-freeze eligibility event. A change committed before the linearization point must be visible to the evaluator. The AssetVersion `FOR UPDATE` lock does not protect the external truth graph — the evaluator must read from a consistent snapshot within the same transaction.

The production implementation acquires `FOR UPDATE` on any local truth-dependency rows (following the global lock order: AssetVersions ascending by id, then LibraryVersions ascending by id, then truth-dependency rows ascending by id) and reads the dependency chain within the same transaction. The `dependency_version_token` is a SHA-256 over the canonical dependency chain state. The production evaluator raises `StaleTruthUnevaluable` until the TruthPackage/MarketAssetBinding ORM models land — the eligibility gate catches this and returns `fail` with `stale_truth_unevaluable`.

Tests use an in-memory adapter returning deterministic results and tokens.

### Explicit limitation

AssetVersions with `truth_dependency_expected=true` cannot be frozen until the TruthPackage/MarketAssetBinding read model lands (the evaluator fails closed with `stale_truth_unevaluable`). AssetVersions with persisted `truth_dependency_expected=false` (grandfathered or explicit) can freeze. This is a real limitation, stated explicitly — not a silent fallback.

---

## §2.5 Post-freeze invalidation and command policy

### Separate event stream

Post-freeze invalidation events are NOT recorded as `ExperimentStateEventRecord` entries — that table represents lifecycle state transitions (`draft` to `frozen` to `running` to `closed`) and is unique by `(experiment_id, revision)`. Using it for invalidation evidence would either violate the revision uniqueness or conflate lifecycle transitions with eligibility changes.

### ExperimentEligibilityEventRecord

Append-only. Records post-freeze eligibility changes with idempotent deduplication.

Fields:

- `id` String(64) PK
- `workspace_id` — composite UNIQUE(workspace_id, id)
- `experiment_id` — composite FK
- `experiment_version_id` — composite FK, NOT NULL for post-freeze eligibility events (the immutable version context is always known after freeze)
- `candidate_id` String(64) — nullable (NULL for version-level events such as LibraryVersion withdrawal affecting the whole experiment). When non-NULL, composite lineage FK `(workspace_id, experiment_version_id, candidate_id) -> experiment_version_candidates(workspace_id, experiment_version_id, id)`, requiring parent uniqueness `UNIQUE(workspace_id, experiment_version_id, id)` on `ExperimentVersionCandidateRecord`. This enforces in the database — not only in service code — that the candidate belongs to the same experiment version as the event. Likewise `(workspace_id, experiment_id, experiment_version_id)` references the matching ExperimentVersion lineage via a composite parent key, so an event cannot combine IDs from different experiments.
- `upstream_event_type` String(40) — `review_rejected`, `qc_failed_blocking`, `product_version_withdrawn`, `offer_version_withdrawn`, `product_version_eligibility_changed`, `offer_version_eligibility_changed`, `stale_truth_detected`, `review_re_approved`, `qc_remediated`
- `upstream_resource_type` String(40) — `quality_review`, `asset_qc_attempt`, `library_version`, `truth_package`, `market_asset_binding`. The vocabulary includes truth-dependency event resources used by the `StaleTruthEvaluator` adapter.
- `upstream_resource_id` String(64) — the triggering record's ID
- `upstream_event_id` String(64) — immutable identity of the upstream event (e.g. the new review's ID, the QC attempt's ID, the LibraryVersion state-change audit ID). Used for deduplication.
- `previous_effective_eligibility` String(20) — `eligible`, `ineligible`, or `unknown`
- `current_effective_eligibility` String(20) — `eligible` or `ineligible`
- `reversible` Boolean
- `evidence_json` JSON — exact upstream state observed
- `observed_at` DateTime(tz)
- `created_at` DateTime(tz)

### Deduplication constraints

Two partial unique constraints keyed on the immutable experiment version and event namespace:

- Candidate-level events: `UNIQUE(workspace_id, experiment_version_id, candidate_id, upstream_event_type, upstream_event_id) WHERE candidate_id IS NOT NULL`
- Version-level events: `UNIQUE(workspace_id, experiment_version_id, upstream_event_type, upstream_event_id) WHERE candidate_id IS NULL`

Including `experiment_version_id` and `upstream_event_type` in both keys ensures deduplication is scoped to the immutable version and event namespace, not merely the mutable experiment aggregate. Event replay is a no-op returning the existing event row; it does not append a duplicate.

### Rebuildable projection

The current-eligibility projection operates at two levels:

**Version-level projection** per `(experiment_version_id)`: starts from baseline eligible, folds version-level events (`candidate_id IS NULL`) in deterministic order `(observed_at ASC, id ASC)`. A version-level invalidation (e.g. `product_version_withdrawn`) applies to the entire ExperimentVersion and all its candidates for start/confirm checks. Candidate-level projection alone is insufficient — commands must check both the version-level and candidate-level projections.

**Candidate-level projection** per `(experiment_version_id, candidate_id)`: starts from baseline eligible, folds candidate-level events (`candidate_id IS NOT NULL`) in deterministic order `(observed_at ASC, id ASC)`.

At each level: reversible invalidation followed by a matching restoration returns eligibility to eligible. Irreversible invalidation is terminal — no restoration event can clear it. A candidate is effectively eligible only when both the version-level and its own candidate-level projections are eligible.

### Reversibility classification

Reversible (a later event may restore eligibility):

- `review_rejected` — can be reversed by `review_re_approved` (new approved review on the same AssetVersion)
- `qc_failed_blocking` — can be reversed by `qc_remediated` (policy-compatible passed attempt with valid remediation link)

Irreversible for the frozen Experiment (requires a new Experiment):

- `product_version_withdrawn`
- `offer_version_withdrawn`
- `product_version_eligibility_changed` (to `blocked`)
- `offer_version_eligibility_changed` (to `blocked`)
- `stale_truth_detected`

### Command policy (preserving statistical evidence)

| Command | On reversible invalidation | On irreversible invalidation |
|---|---|---|
| `start_experiment()` | Hard-block | Hard-block |
| `run_judgement()` | May run. Judgement and Proposal still appended from collected metric data. Proposal records current invalidation reasons in evidence and is marked non-confirmable while any invalidation is active. | May run, same conditions. Preserves historical statistical evidence. |
| `confirm_proposal()` | Rechecks under AssetVersion and LibraryVersion locks. Hard-blocks while any active invalidation remains. If reversible invalidation later clears (re-approval or QC remediation), confirmation may proceed only if proposal freshness/context rules still hold (proposal not superseded, hash still valid); otherwise a new Judgement/Proposal is required. | Hard-block. Irreversible invalidation cannot be cleared. |
| `close_experiment()` | Allowed (closing is always allowed). | Allowed. |

### Expanded recheck scope

`confirm_proposal()` rechecks not only `state='withdrawn'` but:

- LibraryVersion `state` (effective/superseded/withdrawn) and `eligibility` (eligible/warning/blocked)
- Stale-truth dependency status via `StaleTruthEvaluator`
- Effective review and QC status under locks

Publishing a superseding LibraryVersion (creating a new `effective` version, marking the old `superseded`) does NOT by itself invalidate the frozen version — the exact frozen version ID is what matters, and `superseded` state means "not the current version for new uses" but the content is still valid. Only `withdrawn`, `eligibility='blocked'`, or stale dependency invalidate.

### Proposal non-confirmability

`DecisionProposalStateEventRecord` (§1.7) gains an additional event type: `invalidated`. Two distinct scenarios:

**Proposal created during active invalidation.** When `run_judgement()` produces a Proposal while any invalidation is active, it atomically appends both `created` and `invalidated` events. The `invalidated` event records the invalidation reason. This Proposal is terminally non-confirmable — it can never be resurrected, even if the invalidation later clears. A new Judgement/Proposal is required after restoration.

**Proposal created before a later invalidation.** A Proposal that was `created` (pending) before an invalidation event arrives is NOT automatically given a terminal `invalidated` event. Instead:
- Confirmation is blocked while any invalidation is active (the eligibility recheck under locks fails).
- If the invalidation is reversible and later clears (re-approval or QC remediation), confirmation may proceed only if the existing proposal freshness/hash/context checks still pass (proposal not superseded, `proposal_hash` still valid, `expected_proposal_sequence` matches).
- If the invalidation is irreversible, the Proposal receives a terminal `invalidated` event recording the irreversible reason. All open Proposals for the ExperimentVersion receive this event. This transition is idempotent — replaying the invalidation event against an already-invalidated Proposal is a no-op.

**Eligibility event to Proposal state event mapping:**

| Eligibility event | Proposal impact |
|---|---|
| Reversible invalidation (review_rejected, qc_failed_blocking) | Confirmation blocked; no Proposal state event. Proposal stays pending. |
| Reversible restoration (review_re_approved, qc_remediated) | Confirmation unblocked if proposal freshness holds. No Proposal state event. |
| Irreversible invalidation (version withdrawn, eligibility blocked, stale truth) | Terminal `invalidated` event appended to all open Proposals for the ExperimentVersion. Idempotent. |
| Proposal created during any active invalidation | Born terminal: `created` + `invalidated` atomically. |

Confirmation checks the latest Proposal event sequence and rejects any Proposal whose latest event is `invalidated` or `superseded` or `consumed`.

---

## §2.6 Evaluator function design

### New structured evaluator

```python
def evaluate_experiment_eligibility(
    session, ctx, asset_version_id, freeze_context
) -> EligibilityResult:
```

`EligibilityResult`:

- `eligible: bool` — True only if all gates are `pass` or `warn`
- `gates: list[GateResult]` — each with `gate: int`, `code: str`, `outcome: 'pass' | 'warn' | 'fail'`, `primary: bool`
- `warnings: list[str]` — gate codes with `warn` outcome
- `failed_gates: list[str]` — gate codes with `fail` outcome
- `evidence: dict` — the structure from §2.3

`freeze_context` is a `FreezeEligibilityContext` dataclass carrying:

- `experiment_id`, `experiment_revision` — the CAS-locked experiment aggregate
- `product_version_id`, `offer_version_id` — resolved frozen LibraryVersions
- `market`, `placement`, `schedule_start_at`, `schedule_end_at`, `schedule_timezone`
- `surviving_draft_sources: list[dict]` — ordered surviving draft entries identified by `source_entry_id` and stable source fingerprint (not by not-yet-created frozen candidate IDs)
- `draft_execution_modes: dict[str, str]` — each surviving entry's `execution_mode`
- `resolution_plan: list[dict]` — the submitted concept-resolution plan (command-scoped input)
- `resolution_plan_hash: str` — canonical hash of the resolution plan
- `product_profile_entry_id: str` — the bridged product profile

The same immutable in-memory context is passed to every candidate evaluation. Gate 12 (execution mode uniformity) and each candidate's Gate 15 (concept resolution completeness) are derived from it. Gate 16 remains one version-level result computed from `surviving_draft_sources`.

When `freeze_context=None`, only advisory gates (1, 2, 4, 5) are evaluated.

### Compatibility wrapper

`is_ready_for_experiment(session, ctx, asset_version_id) -> bool` remains unchanged in signature. Internally it calls `evaluate_experiment_eligibility()` with `freeze_context=None` (advisory mode) and returns `.eligible`. Old callers and tests continue to work — a `bool` is never truthy-when-false. An `EligibilityResult` object would be truthy even when `eligible=False` unless special behavior is added; this wrapper avoids that trap.

Migration to the structured evaluator is per-caller, not a flag-day.

### Batch freeze evaluation

`freeze_experiment()` calls `evaluate_experiment_eligibility()` per candidate with the full `freeze_context`, then checks gate 16 (candidate count) at version level. All gate results are collected and reported in a structured freeze error if any candidate fails.

---

## §2.7 Evaluation ordering relative to concept resolution

1. Draft entries added/removed/updated — advisory eligibility per entry (gates 1, 2, 4, 5). Each entry mutation increments `ExperimentRecord.revision`.
2. Concept detection identifies duplicate groups across all Draft entries (client-side or advisory endpoint).
3. User prepares a concept-resolution plan specifying actions per duplicate group (allow, exclude, remap). The plan is not persisted as a draft mutation — it is command-scoped input to freeze.
4. Freeze begins (resolution plan submitted with `expected_revision` and request fingerprint):
   a. Lock ExperimentRecord with CAS on `expected_revision`. Lock ExperimentEntryRecords in id order. This prevents concurrent draft edits from being silently omitted.
   b. Validate the submitted resolution plan against the locked canonical entry set and derived duplicate groups.
   c. Resolve the canonical surviving entry set and build `FreezeEligibilityContext` using `source_entry_id` and stable source fingerprints (not frozen candidate IDs, which do not exist yet).
   d. Excluded entries are recorded in resolution evidence, not frozen as active candidates.
   e. Lock AssetVersionRecords, LibraryVersionRecords, and truth-dependency rows per the global lock order (§2.1).
   f. Remaining entries undergo full authoritative eligibility (all 15 candidate-level gates) under locks.
   g. All remaining entries must pass. If any fail, freeze aborts — no partial freeze.
   h. Gate 15 (concept resolution) verifies all groups involving remaining entries are resolved per the submitted plan.
   i. Gate 16 (candidate count) verifies `2 ≤ surviving_entry_count ≤ 100` after resolution; below 2 fails with `insufficient_candidates_after_resolution`, above 100 fails with `candidate_limit_exceeded`.
5. Hash chain computed, canonical snapshot assembled. `ExperimentVersionRecord`, `ExperimentVersionCandidateRecord`, `ExperimentVersionCandidateConceptRecord`, and `ExperimentVersionConceptResolutionRecord` rows generated and inserted atomically. State CAS (`UPDATE ... WHERE revision = expected`) in the same transaction. There is no pre-freeze lock on those frozen rows — they are created here.

---

## §2.8 Deferred (not defined in Section 2)

- Detailed API request/response shapes for freeze error reporting — Section 3 (lifecycle/APIs).
- ExperimentStateEventRecord entries for lifecycle transitions triggered by invalidation — Section 3.
- Cross-experiment primary winner policy projection rules — deferred to separate policy design (stated in §1.10).


<!-- source-boundary: section3-final -->
# Section 3: Lifecycle State Machine, API Commands, and Replay

## §3.1 New-path discriminator

### `ExperimentRecord.record_schema_version`

Add nullable `record_schema_version` Integer to `ExperimentRecord`:

- Existing rows backfill/remain NULL — legacy path.
- Every new #22 experiment is created with `record_schema_version = 2`.

Every command and read endpoint routes by this discriminator, not by ExperimentVersionRecord presence (which cannot distinguish a new-path Draft from a legacy experiment, since the version row does not exist until freeze).

`CHECK(record_schema_version IS NULL OR record_schema_version = 2)` prevents invalid values. The cross-table cardinality invariant (schema-2 Draft has no ExperimentVersionRecord; frozen/running/closed has exactly one) is enforced by service logic plus the existing `UNIQUE(experiment_id)` on ExperimentVersionRecord.

### URL routing

URLs fall into two categories:

**Shared URLs** — `GET /api/experiments/{id}`, `POST .../freeze`, `POST .../judge`, `POST .../close` — load the tenant-scoped Experiment and dispatch internally by `ExperimentRecord.record_schema_version` to the legacy or schema-2 module implementation. Both schema paths share these URLs; the handler inspects the loaded record and calls the appropriate service function.

**Exclusive URLs** — reject the wrong schema with `409 schema_version_mismatch`:

| URL | Schema |
|---|---|
| `POST /api/experiments/{id}/entries/{entry_id}/bind` | Legacy only (NULL) |
| `POST /api/experiments/{id}/entries` | Schema-2 only |
| `PUT /api/experiments/{id}/entries/{entry_id}` | Schema-2 only |
| `DELETE /api/experiments/{id}/entries/{entry_id}` | Schema-2 only |
| `PUT /api/experiments/{id}/context` | Schema-2 only |
| `POST /api/experiments/{id}/candidates/{cid}/bind` | Schema-2 only |
| `POST /api/experiments/{id}/start` | Schema-2 only |
| `POST /api/experiments/{id}/proposals/{pid}/confirm` | Schema-2 only |

### Create cutover

After the migration, `POST /api/experiments` accepts only the schema-2 `draft_context` create shape. The server always sets `record_schema_version = 2` — the client never supplies this persistence discriminator. The old `config + inline entries` create shape is rejected with `422 legacy_create_shape_retired`.

Existing NULL-schema experiments remain routable through the legacy service implementations until they complete their lifecycle. Compatibility is lifecycle compatibility for existing records, not continued legacy creation.

### Schema-2 ExperimentVersion cardinality

For `record_schema_version = 2`: Draft has no ExperimentVersionRecord; frozen/running/closed has exactly one.

### `set_verdict()` retirement

`set_verdict()` is explicitly retired. It performs no new write and returns a typed deprecation error (`410 verdict_endpoint_retired`). Legacy reads only read legacy rows; new-path reads only read schema-2 decisions/projections. The two paths never merge or "prefer" rows by coincidence.

---

## §3.2 Idempotency and precondition protocol

### `Idempotency-Key` header

All new-path mutations require the `Idempotency-Key` HTTP header — not a body field. The existing `app.platform.idempotency` module (`compute_fingerprint()`, `create_or_replay_intent()`) is the shared authority.

The **combined idempotency identity** binds actor, workspace, command type, and the full request fingerprint. These are bound at different layers:

- `compute_fingerprint(method, operation_id, path, query, body, precondition_headers)` hashes the request shape: HTTP method, command identity, path, query, canonical body, and precondition headers (`If-Match`). It does not hash actor or workspace.
- The idempotency record/reservation protocol separately binds `(key_hash, workspace_id)` as the reservation scope and persists `actor_id` and `command_type` for reuse detection. `_check_binding()` verifies actor/command match on replay.

This matches the existing platform interface exactly; no changes to `compute_fingerprint()` are needed.

### `If-Match` for aggregate revision and Proposal sequence

`If-Match` is the HTTP representation of aggregate revision (for Experiment mutations) and Proposal sequence (for confirmation). No competing `expected_revision` or `expected_proposal_sequence` body field exists.

The domain service receives the parsed expected value from the HTTP layer. Responses on mutable projections include `ETag` carrying the current revision or sequence.

### Replay semantics (applied to every new-path mutation)

| Scenario | Result |
|---|---|
| Same key + same actor/workspace/command + same fingerprint | Return the original committed outcome, even if the resource is now frozen/consumed/closed or the actor's authorization later changed. Idempotency replay is checked before current state/authorization/eligibility guards. |
| Same key + different actor, command, or fingerprint | `409 idempotency_key_reused` (detected by `_check_binding()` on the reservation row) |
| Different key | Full guards (state, authorization, eligibility, preconditions) |

Natural UNIQUE constraints (e.g. one binding per candidate, snapshot hash uniqueness) remain defense-in-depth, not the replay protocol.

### Status code mapping

| Code | Meaning |
|---|---|
| `201` | Resource created (create experiment, freeze, bind, judge, confirm) |
| `200` | Idempotent replay returning existing outcome; read endpoints; updates |
| `404` | Tenant-scoped absence — experiment/candidate/proposal not found within workspace |
| `409` | Revision/state/current-eligibility conflict; idempotency key reuse; schema version mismatch; candidate limit exceeded |
| `410` | Retired endpoint (`set_verdict`) |
| `422` | Intrinsically invalid request content — malformed resolution plan, invalid market code, schema validation failure, `legacy_create_shape_retired` |
| `400` | Missing `Idempotency-Key` header — `idempotency_key_required` |
| `428` | Missing `If-Match` header on a command that requires it — `precondition_required` |

### Commands using this protocol

Create, every Draft edit (add/update/remove candidate entry, update Draft context), freeze, bind-candidate, start, judge, confirm, and close. Each requires `Idempotency-Key`; each mutation that touches a revisioned aggregate additionally requires `If-Match`.

---

## §3.3 Draft context and Draft commands

### Single authority for pre-freeze context

Schema-2 `ExperimentRecord` owns one typed mutable Draft context — the existing `config_json` column evolves to contain the complete pre-freeze context:

```
draft_context = {
    "schema_version": 2,
    "product_version_id": "...",
    "offer_version_id": "...",
    "market": "US",                          # ISO 3166-1 alpha-2
    "currency": "USD",                       # ISO 4217
    "placement": "feed",                     # controlled vocabulary
    "audience": {
        "schema_version": "...",
        "content": { ... }
    },
    "budget": {
        "daily_amount": "100.000000",        # Decimal string, Numeric(18,6)
        "total_amount": "3000.000000"
    },
    "schedule": {
        "start_at": "2026-09-10T00:00:00Z",
        "end_at": "2026-09-24T23:59:59Z",
        "timezone": "America/New_York"       # IANA
    },
    "attribution": {
        "click_window_days": 7,              # 1–90
        "view_window_days": 1                # 1–90
    },
    "statistical_plan": {
        "schema_version": "...",
        "primary_metric": "ctr",
        "ctr_threshold": "0.020000",
        "alpha": "0.050000",
        "bootstrap_samples": 2000,
        "min_days": 3,
        "min_impressions": 1000,
        "min_clicks": 20,
        "min_purchases": "3.000000",
        "require_fresh_data": true
    },
    "marketing_vars_schema_version": "..."
}
```

`audience.content_hash` and `statistical_plan.content_hash` are never client assertions — the server recomputes them at canonicalization time and rejects any client-supplied value that does not match the recomputed hash. These hashes are derived, not trusted inputs.

Product/Offer version selections are owned by Draft context. Freeze reads this locked Draft context and accepts only the command-scoped `concept_resolution_plan` (plus `Idempotency-Key` and `If-Match` headers). No overlapping fields exist in FreezeInput.

### Canonical validation rules

| Field | Validation |
|---|---|
| `market` | ISO 3166-1 alpha-2, uppercase, exists in platform market vocabulary |
| `currency` | ISO 4217, uppercase, 3 characters |
| `placement` | Controlled vocabulary: `feed`, `story`, `reel`, `search`, `marketplace` (extensible via platform config) |
| `budget.daily_amount`, `budget.total_amount` | Decimal string, Numeric(18,6) precision/scale, > 0, `total_amount >= daily_amount`. Currency-specific display scale (e.g. 2dp for USD) is a presentation concern; storage and hashing use full 6dp canonical form. |
| `schedule.timezone` | IANA timezone identifier |
| `schedule.start_at`, `schedule.end_at` | ISO 8601 UTC, `end_at > start_at` |
| `attribution.click_window_days` | Integer, 1–90 |
| `attribution.view_window_days` | Integer, 1–90 |
| `statistical_plan.alpha` | Decimal string, 0 < α < 1, Numeric(18,6) |
| `statistical_plan.bootstrap_samples` | Integer, 200–100000 |
| All Decimal fields | Decimal string representation with Numeric(18,6) canonical form — no binary float enters hashes |

Create and update-Draft-context validate and canonicalize all fields. Hash inputs use canonical Decimal string representations at full 6dp scale.

### Draft command surface

All Draft commands are schema-2 only, tenant-scoped, idempotent via `Idempotency-Key`, `If-Match` protected, and increment `ExperimentRecord.revision`.

#### Create experiment

`POST /api/experiments` with the schema-2 `draft_context` body shape (§3.1 — the old `config + inline entries` shape is rejected with `422 legacy_create_shape_retired`). The server sets `record_schema_version = 2`; the client never supplies this discriminator.

Creates `ExperimentRecord` in `draft` state with `revision = 1` and validated Draft context. Requires at least the Product/Offer selection and statistical plan. Draft may be created with zero candidate entries — the minimum-2 constraint is enforced at freeze, not create. Returns `ETag` with `revision = 1`.

#### Add candidate entry

`POST /api/experiments/{id}/entries`

Adds one `ExperimentEntryRecord` to the Draft. Input: `asset_version_id`, `marketing_vars` (versioned bounded schema). Validates AssetVersion exists within workspace, Asset belongs to Experiment's Product. Enforces the candidate cap: at most 100 active Draft entries per Experiment — returns `409 candidate_limit_exceeded` if the aggregate is already at the cap. Increments revision. Advisory eligibility (gates 1, 2, 4, 5) returned as warnings.

#### Update candidate entry

`PUT /api/experiments/{id}/entries/{entry_id}`

Replaces the entry's `asset_version_id` and/or `marketing_vars`. Clear replace semantics — the entire entry is overwritten, not patched. Same validation as add. Increments revision.

#### Remove candidate entry

`DELETE /api/experiments/{id}/entries/{entry_id}`

Removes the entry from Draft. Draft may temporarily contain fewer than 2 candidates. Increments revision.

#### Update Draft context

`PUT /api/experiments/{id}/context`

Updates the mutable Draft context fields (market, currency, placement, audience, budget, schedule, attribution, statistical plan, marketing-vars schema version). Full canonical validation. Increments revision. Product/Offer version selection may also be changed here while in Draft.

---

## §3.4 Lifecycle state machine

### States

```
draft → frozen → running → closed
```

Same four states as today. `record_schema_version = 2` experiments use the new-path commands below; legacy experiments use existing commands unchanged.

### Transitions

| Transition | Command | Preconditions |
|---|---|---|
| `draft → frozen` | `freeze_experiment()` | `If-Match` revision; all 16 eligibility gates pass or warn; resolution plan valid |
| `frozen → running` | `start_experiment()` | Post-freeze validity check passes (§3.7); exactly one binding per active candidate; binding-set hash recorded |
| `frozen → closed` | `close_experiment()` | `closure_kind = abandoned_before_start` — no Judgement required |
| `running → closed` | `close_experiment()` | `closure_kind = completed` requires at least one Judgement (not necessarily a material Decision); `closure_kind = aborted` or `invalidated` does not |
| `frozen → closed` | `close_experiment()` | `closure_kind = aborted` or `invalidated` — with reason/evidence |

### `closure_kind` vocabulary

`CloseInput` carries a bounded `closure_kind` and reason:

| Kind | Allowed from | Requires Judgement? | Use case |
|---|---|---|---|
| `completed` | `running` only | Yes (at least one) | Normal completion — an evidence-complete experiment may be all-inconclusive (no material Decisions) |
| `abandoned_before_start` | `frozen` only | No | Experiment frozen but never started; no bindings sent |
| `aborted` | `frozen` or `running` | No | Operational abort; exact reason/evidence recorded |
| `invalidated` | `frozen` or `running` | No | Closed due to irreversible invalidation; requires an active irreversible `ExperimentEligibilityEventRecord` on the same ExperimentVersion |

The single `closed` state is preserved. Closure kind belongs in the `ExperimentStateEventRecord` evidence and the experiment's close projection — not in a separate terminal state.

---

## §3.5 Freeze command

`POST /api/experiments/{id}/freeze`

Headers: `Idempotency-Key`, `If-Match: <revision>`.

Body (schema-2 `FreezeInput`):

```python
class FreezeInput(BaseModel):
    concept_resolution_plan: list[ConceptResolutionAction] = Field(default_factory=list)
```

The resolution plan is the only body field — all other freeze inputs (Product/Offer versions, market, schedule, statistical plan) come from the locked Draft context. The resolution plan is validated against the locked canonical entry set and derived duplicate groups.

### Freeze procedure

1. Check/claim idempotency reservation before state/authorization/eligibility guards.
2. Lock `ExperimentRecord` with CAS on `If-Match` revision. Lock `ExperimentEntryRecord` rows in id ASC order.
3. Validate `record_schema_version = 2`, `state = 'draft'`.
4. Read and lock Draft context. Validate resolution plan against locked entry set and derived duplicate groups.
5. Resolve surviving entry set. Build `FreezeEligibilityContext` using `source_entry_id` and stable source fingerprints.
6. Lock AssetVersionRecords, LibraryVersionRecords, truth-dependency rows per global lock order (§2.1).
7. Evaluate all 16 eligibility gates (§2.3) under locks. All must pass or warn.
8. Compute hash chain, assemble canonical snapshot.
9. Insert `ExperimentVersionRecord`, `ExperimentVersionCandidateRecord`, `ExperimentVersionCandidateConceptRecord`, `ExperimentVersionConceptResolutionRecord` atomically with snapshot.
10. CAS update `ExperimentRecord` (state → frozen, revision + 1) in same transaction.
11. Append `ExperimentStateEventRecord` (draft → frozen).
12. Commit idempotency record.
13. Return frozen representation with `ETag: <new_revision>`.

Idempotent replay: same `Idempotency-Key` + same fingerprint returns the existing frozen outcome without re-running gates.

---

## §3.6 Bind candidate command

`POST /api/experiments/{id}/candidates/{candidate_id}/bind`

Headers: `Idempotency-Key`, `If-Match: <experiment_revision>`.

Body:

```python
class BindCandidateInput(BaseModel):
    ad_account_id: str
    ad_id: str
    source: str = "manual_read_only"
```

### Bind procedure

1. Check/claim idempotency reservation (same-key replay returns existing binding before any guard).
2. Lock Experiment aggregate. Verify `If-Match` revision, `record_schema_version = 2`, `state = 'frozen'`.
3. Lock the candidate/binding identity rows. This serializes bind with `start_experiment()` — a new binding cannot commit after start has recorded its binding-set hash.
4. Validate candidate belongs to this Experiment's version.
5. Check for existing binding on this candidate; if identical, return it (natural idempotency). If different, `409 binding_conflict`.
6. Validate `ad_account_id` references a `MetaAccountRecord` within the same workspace.
7. Insert binding. Bind does not increment Experiment revision — the binding set is a child collection, not the aggregate header.
8. Commit idempotency record.
9. Return binding representation.

### Binding rules

- New binding creation is allowed only while `state = 'frozen'`. After `running`, an identical idempotent replay (same key + same fingerprint) may return the existing binding, but no new or changed binding can be created.
- Each active candidate may have at most one binding. Attempting to bind a candidate that already has a different binding returns `409 binding_conflict`.
- `ad_account_id` must reference a `MetaAccountRecord` within the same workspace — tenant-scoped identity validation.
- Bindings reference `experiment_version_candidate_id` (not legacy `experiment_entry_id`). For `record_schema_version = 2`, `experiment_entry_id` is NULL (§1 mandate).
- Binding uniqueness is tenant-scoped: `UNIQUE(workspace_id, ad_account_id, ad_id)` — not global.

---

## §3.7 Start command

`POST /api/experiments/{id}/start`

Headers: `Idempotency-Key`, `If-Match: <revision>`.

New command: transitions `frozen → running`.

### Start procedure

1. Check/claim idempotency reservation.
2. Lock Experiment aggregate (class 1): ExperimentRecord, version, candidates, bindings.
3. Validate `record_schema_version = 2`, `state = 'frozen'`, `If-Match` revision.
4. Lock AssetVersionRecords (class 2), LibraryVersionRecords (class 3), truth-dependency rows (class 4), eligibility event/projection rows (class 5) — all in global lock order (§2.1).
5. **Post-freeze validity check** (distinct from freeze eligibility — see §3.11):
   - Recompute from authoritative rows under the locks acquired in steps 2–4.
   - Treat the eligibility event projection (class 5) as audit/read acceleration, not the sole guard.
   - Allow `superseded` LibraryVersion state (§2.5: superseded does not invalidate the exact frozen version).
   - Check: withdrawal/blocked eligibility, stale truth, effective review/QC.
   - Hard-block on any invalidity.
6. Load and validate all bindings:
   - Exactly one binding per active candidate — missing binding blocks start.
   - Each binding's `ad_account_id` references a valid tenant-scoped Meta account.
7. Compute canonical binding-set hash over `(candidate_id, ad_account_id, ad_id)` tuples sorted by `candidate_id`.
8. Record binding-set hash in the start `ExperimentStateEventRecord` evidence.
9. CAS update: `state = 'running'`, `revision + 1`, `started_at = now`.
10. Return running representation with `ETag: <new_revision>`.

---

## §3.8 Judge command

`POST /api/experiments/{id}/judge`

Headers: `Idempotency-Key`, `If-Match: <experiment_revision>`.

Body:

```python
class JudgeInput(BaseModel):
    as_of: date | None = None
```

### Judge procedure

1. Check/claim idempotency reservation (same-key replay returns existing Judgement+Proposal before any guard).
2. Lock Experiment aggregate (class 1). Verify `If-Match` revision, `record_schema_version = 2`, `state = 'running'`. This serializes judge with close — a Judgement cannot be appended after the Experiment became closed. Judge does not increment Experiment revision, but the precondition and lock are authoritative.
3. Lock AssetVersionRecords (class 2), LibraryVersionRecords (class 3), truth-dependency rows (class 4), eligibility event/projection rows (class 5) — in global lock order (§2.1).
4. **Post-freeze validity check** under locks acquired in steps 2–3:
   - Judge records current invalidity reasons but still produces evidence (§2.5).
   - The Judgement preserves collected metric data regardless of eligibility status.
5. Collect metric data from bound ad IDs via `AdDailyMetricRecord`, using the frozen binding-set hash to bind to the exact immutable binding set recorded at start.
6. Build judge input snapshot keyed by `experiment_version_candidate_id` (not legacy entry IDs).
7. Call `judge(snapshot)` — pure deterministic function, unchanged algorithm.
8. Compute snapshot hash and algorithm version.

### Judgement uniqueness and replay

Tenant-scoped Judgement uniqueness: `UNIQUE(workspace_id, experiment_id, snapshot_hash, algorithm_version)`. One ExperimentVersion per Experiment means `experiment_id` is sufficient without `experiment_version_id` in this constraint.

Concurrent unique-conflict handling: if INSERT conflicts, load and return the winner rather than raising — this is the natural replay for identical metric snapshots.

### Proposal creation and cardinality

Each Judge command creates or replays exactly one Proposal for `(judgement_id, eligibility_context_hash)`. A Judgement may own multiple immutable Proposals across distinct eligibility contexts.

The transaction (continuing from judge steps 1–8, classes 1–5 already held):

1. Create or replay the Judgement first (snapshot hash uniqueness).
2. Compute the authoritative `eligibility_context_hash` under classes 1–5 locks — SHA-256 of: ExperimentVersion frozen hash + current version-level eligibility projection head + per-candidate eligibility projection heads + authoritative recheck evidence from the post-freeze validity check.
3. Lock Proposal aggregate (class 6): acquire the Proposal row if it exists.
4. Insert-or-load the uniquely identified Proposal via `UNIQUE(workspace_id, judgement_id, eligibility_context_hash)`.

The Proposal hash commits this `eligibility_context_hash` alongside the Judgement hash and per-candidate verdicts.

Same-key HTTP replay returns the originally committed Judgement+Proposal pair even if a newer eligibility context now exists. A new key may select or create the Proposal for the current context.

### Proposal and eligibility restoration

If a Proposal was born terminally invalidated (created during active invalidation per §2.5) and eligibility later restores, rerunning judge with a new key may reuse the same Judgement (same snapshot hash) but creates a new Proposal under the new `eligibility_context_hash`. This is how §2's "new Judgement/Proposal command" becomes possible without falsifying metric identity.

Same identity under concurrency resolves to the unique winner. Irreversible invalidation still makes every Proposal non-confirmable.

### Judge does not create Decisions

The judge command produces only a Judgement + Proposal. Material `ExperimentDecisionRecord` rows are created exclusively by confirmation (§3.9). This is the core separation from the legacy path, which directly writes Decisions from judge output.

---

## §3.9 Confirm proposal command

`POST /api/experiments/{id}/proposals/{proposal_id}/confirm`

Headers: `Idempotency-Key`, `If-Match: <proposal_sequence>`.

### Confirmation ordering

Replay resolution is checked before "latest event is created" — a legitimate retry of a consumed Proposal returns the existing Confirmation/Decisions rather than failing with a `consumed` error.

### New-key confirmation procedure (single transaction)

1. Check/claim idempotency reservation.
2. For a new idempotency key, one transaction following the global lock order (§2.1):
   a. Lock Experiment aggregate (class 1): ExperimentRecord, version, candidates.
   b. Lock AssetVersions, LibraryVersions, truth-dependency rows (classes 2–4).
   c. Lock eligibility event/projection rows (class 5).
   d. Lock Proposal aggregate (class 6): `DecisionProposalRecord` and latest Proposal state event.
   e. Verify tenant/experiment/version lineage — Proposal belongs to a Judgement belonging to this Experiment's version.
   f. Verify `proposal_hash` and `If-Match` proposal sequence.
   g. Verify current actor role + authorization epoch (checked in the same transaction, evidence persisted as defined in §1).
   h. Verify all-inconclusive rule: if every candidate verdict is `inconclusive`, confirmation is blocked — `inconclusive` is an evidence status, never material.
   i. **Post-freeze validity check** under locks: hard-block on any active invalidity (§2.5, §3.11).
   j. Insert `DecisionConfirmationRecord` with frozen authorization evidence.
   k. Insert `consumed` event on `DecisionProposalStateEventRecord`.
   l. Insert material `ExperimentDecisionRecord` rows — one per candidate with a non-inconclusive verdict.
   m. Update `ContextualVerdictProjection` rows with deterministic ordering `(confirmed_at DESC, decision_id DESC)`.
   n. Commit.
3. Same-key replay returns exactly those stored IDs without reapplying effects.
4. Different-key confirmation of a consumed/invalidated/superseded Proposal returns `409`.

### Authorization matrix

| Command | Required permission | Additional constraints |
|---|---|---|
| Create experiment | `experiment.create` | — |
| Add/update/remove entry | `experiment.edit` | Draft only |
| Update Draft context | `experiment.edit` | Draft only |
| Freeze | `experiment.freeze` | Draft only |
| Bind candidate | `experiment.bind` | Frozen only |
| Start | `experiment.start` | Frozen only |
| Judge | `experiment.judge` | Running only |
| Confirm | `experiment.confirm` | Running only; current authorization epoch checked in-transaction; resolved current role (`owner`/`member`) and epoch persisted in confirmation evidence |
| Close | `experiment.close` | Frozen or running |

All commands additionally require workspace membership and tenant scope matching the Experiment's `workspace_id`. Values such as `experiment.confirm` are permissions, not roles. Confirmation separately persists the resolved current role and authorization epoch as evidence (§1).

---

## §3.10 Close command

`POST /api/experiments/{id}/close`

Headers: `Idempotency-Key`, `If-Match: <revision>`.

Body:

```python
class CloseInput(BaseModel):
    closure_kind: Literal["completed", "abandoned_before_start", "aborted", "invalidated"]
    reason: str = Field(max_length=1000)
    evidence: dict = Field(default_factory=dict)
```

### Close procedure

1. Check/claim idempotency reservation.
2. Validate `record_schema_version = 2`, `If-Match` revision.
3. Validate `closure_kind` against current state:
   - `completed`: `running` only; verify at least one Judgement exists.
   - `abandoned_before_start`: `frozen` only; no Judgement required.
   - `aborted`: `frozen` or `running`; no Judgement required.
   - `invalidated`: `frozen` or `running`; no Judgement required; requires an active irreversible `ExperimentEligibilityEventRecord` belonging to the same ExperimentVersion — the service verifies this before accepting the close.
4. CAS update: `state = 'closed'`, `revision + 1`, `closed_at = now`.
5. Append `ExperimentStateEventRecord` with `closure_kind`, `reason`, and `evidence` in the event details.
6. Return closed representation with `ETag: <new_revision>`.

Idempotent replay of an already-closed experiment with the same key returns the existing closed state.

---

## §3.11 Post-freeze validity (distinct from freeze eligibility)

### Motivation

Freeze eligibility (§2.3) requires Product/Offer versions to be current `effective`. After freeze, §2.5 explicitly allows `superseded` — a newer version existing does not invalidate the exact frozen version. Reusing all 16 freeze gates unchanged for start/judge/confirm would incorrectly block on `superseded`.

### Post-freeze validity evaluation

A distinct evaluation mode/interface that checks the frozen exact versions under locks:

| Check | Pass condition | Failure |
|---|---|---|
| LibraryVersion withdrawal | Neither frozen ProductVersion nor OfferVersion `state = 'withdrawn'` | Hard-block |
| LibraryVersion eligibility | Neither frozen ProductVersion nor OfferVersion `eligibility = 'blocked'` | Hard-block |
| Stale truth | For `truth_dependency_expected=false`: `not_applicable` from persisted evidence (no evaluator call). For `truth_dependency_expected=true`: `StaleTruthEvaluator` returns `clear`. | Hard-block |
| Effective review | Latest review is `approved` (§2.1 semantics) | Hard-block (start/confirm); recorded but allowed (judge) |
| Effective QC | No active `pending`/`running` attempt AND no unresolved blocking/remediation-invalid state (§2.2 full evaluation) | Hard-block (start/confirm); recorded but allowed (judge) |
| Eligibility event projections | Version-level + candidate-level projections both eligible | Hard-block (start/confirm); recorded but allowed (judge) |

Crucially: `superseded` LibraryVersion state is allowed. Only `withdrawn` or `eligibility = 'blocked'` is an invalidation. The effective QC check includes the `qc_in_progress` gate — an active attempt blocks start/confirm just as an unresolved block does.

### Recomputation vs. projection

The post-freeze validity check recomputes from authoritative rows under locks. The eligibility event projection serves as audit trail and read acceleration, but is not the sole guard — the authoritative recheck prevents stale projections from allowing or blocking incorrectly.

### Per-command behavior

| Command | On invalidity |
|---|---|
| `start_experiment()` | Hard-block |
| `run_judgement()` | Records invalidity in evidence; still produces Judgement + Proposal. Proposal may be born terminally invalidated (§2.5). |
| `confirm_proposal()` | Hard-block |

---

## §3.12 Read endpoints

### Bounded representations

`GET /api/experiments/{id}` must not embed unbounded Judgement, Proposal, Decision, eligibility-event, or full evidence histories.

Return:

- Experiment summary: id, name, state, record_schema_version, revision, Draft context or frozen version summary.
- Complete candidate list (bounded by the 100-candidate cap enforced at add and freeze): id, source_entry_id, asset_version summary, marketing_vars, binding summary, current eligibility status.
- Current eligibility/proposal summaries: latest version-level eligibility, latest proposal state, counts.
- `ETag` with current revision.
- Links/cursors to paginated collections.

### Paginated collection endpoints

| Endpoint | Content |
|---|---|
| `GET /api/experiments/{id}/judgements` | Cursor-paginated judgement list |
| `GET /api/experiments/{id}/proposals` | Cursor-paginated proposal list with latest state |
| `GET /api/experiments/{id}/decisions` | Cursor-paginated decision list |
| `GET /api/experiments/{id}/eligibility-events` | Cursor-paginated eligibility event list |
| `GET /api/experiments/{id}/verdicts` | Cursor-paginated contextual verdict projection |

Each paginated endpoint uses cursor-based pagination with a bounded page size.

### Projection response metadata

Every projection response includes:

- `record_schema_version` — discriminator.
- Relevant immutable hashes: `frozen_snapshot_hash`, `proposal_hash`, `eligibility_context_hash` where applicable.
- Current `ETag` / revision or Proposal sequence — for precondition headers on subsequent mutations.
- `last_eligibility_event_cursor` — so clients can reason about freshness of eligibility state.

### Evidence endpoints

Exact evidence endpoints (e.g. `GET /api/experiments/{id}/judgements/{jid}/evidence`) are authorization-aware. Evidence may be redacted where the actor lacks the required evidence-read role.

### Replay response equivalence

Replay responses are observationally equivalent to the original committed result — same resource IDs and semantic status. They are not necessarily a newly recomputed full representation. The committed IDs and status are returned from the idempotency record, not recomputed.

---

## §3.13 Legacy transition

### Coexistence

Legacy and new-path experiments coexist in the same database. `record_schema_version` is the routing discriminator. After the migration, only schema-2 experiments can be created (§3.1 — old create shape rejected with `422 legacy_create_shape_retired`). Existing NULL-schema experiments remain routable through legacy service implementations until they complete their lifecycle. There is no migration of existing experiments from legacy to schema-2.

### Legacy endpoint preservation

Legacy `bind_entry()`, `run_judgement()` (which writes Decisions directly), and `close_experiment()` remain functional for existing NULL-schema experiments. They reject schema-2 experiments with `409 schema_version_mismatch`.

### `set_verdict()` deprecation

Returns `410 verdict_endpoint_retired` for all experiments regardless of schema version. No new writes.

---

## §3.14 Deferred (not defined in Section 3)

- Database migration DDL and backfill strategy — Section 5.
- Automated evidence generation and audit trail details — Section 5.
- Cross-experiment primary winner policy projection — deferred to separate policy design (§1.10).
- Detailed Draft context JSON schema evolution and versioning strategy — implementation detail.
- Exact authorization role definitions and RBAC configuration — platform concern, referenced by matrix above.
- Worker/async command paths for long-running operations — deferred unless a specific command requires async.

---

## Coverage map for Section 3 review corrections

### Round 1 (8 corrections)

| # | Correction | Applied in |
|---|---|---|
| 1 | Explicit `record_schema_version` discriminator at creation; legacy/new-path routing; `set_verdict()` retirement | §3.1 |
| 2 | `Idempotency-Key` header (not body); `If-Match` for revision/sequence; `compute_fingerprint()` integration; replay before guards; status code mapping | §3.2 |
| 3 | Single Draft context authority; no duplicate fields in FreezeInput; Draft commands (add/update/remove entry, update context); versioned bounded schemas with canonical validation | §3.3, §3.5 |
| 4 | `closure_kind` with bounded vocabulary; `abandoned_before_start` for pre-start closure; bind only while frozen; start locks/validates bindings and computes binding-set hash | §3.4, §3.6, §3.7, §3.10 |
| 5 | Separate post-freeze validity from freeze eligibility; allow `superseded`; per-command invalidity behavior | §3.11 |
| 6 | `eligibility_context_hash` on Proposal; `UNIQUE(workspace_id, judgement_id, eligibility_context_hash)`; restored eligibility creates new Proposal under new hash | §3.8 |
| 7 | Confirmation replay before state check; single-transaction atomicity with global lock order; authorization matrix for all commands | §3.9 |
| 8 | Bounded read representations; paginated collection endpoints; projection metadata (hashes, ETag, eligibility cursor); replay response equivalence | §3.12 |

### Acceptance fixes (6 corrections)

| # | Correction | Applied in |
|---|---|---|
| A1 | Shared URLs dispatch by `record_schema_version`; exclusive URLs reject wrong schema; post-migration create cutover — only schema-2 `draft_context` shape accepted, old shape rejected with `422 legacy_create_shape_retired`; server sets discriminator, not client; CHECK constraint | §3.1 |
| A2 | Bind and judge require `If-Match: <experiment_revision>`; bind serializes with start (lock candidate/binding); judge serializes with close (lock Experiment) | §3.6, §3.8 |
| A3 | Attribution uses `click_window_days`/`view_window_days` (matches §1 frozen contract); server recomputes content hashes (never trusted client assertions); Decimal precision Numeric(18,6) not hard-coded 2dp; `Field(default_factory=list)` for resolution plan | §3.3, §3.5 |
| A4 | Post-freeze QC validity includes active `pending`/`running` attempts (not just unresolved blocks); truth-independent AVs return `not_applicable` from persisted evidence without evaluator call | §3.11 |
| A5 | Proposal cardinality: each judge command creates/replays one Proposal per `(judgement_id, eligibility_context_hash)`; a Judgement may own multiple Proposals; transaction: Judgement first → compute hash under locks → insert-or-load Proposal | §3.8 |
| A6 | `400 idempotency_key_required` / `428 precondition_required` (not ambiguous); "Required permission" column (not role); `completed` does not require material Decision; `invalidated` requires active irreversible eligibility event | §3.2, §3.4, §3.9, §3.10 |

### Final consistency fixes (4 corrections, cross-section)

| # | Correction | Applied in |
|---|---|---|
| C1 | Create cutover unambiguous: only schema-2 create accepted after migration; old shape → `422 legacy_create_shape_retired`; no dual-shape dispatch | §3.1, §3.3, §3.13, §3.2 status table |
| C2 | `compute_fingerprint()` does not hash actor/workspace — those are bound by reservation `(key_hash, workspace_id)` + `actor_id`/`command_type` columns; replay table corrected | §3.2 |
| C3 | One cross-section global lock order (6 classes): Experiment → AssetVersion → LibraryVersion → truth → eligibility → Proposal; §2.1 command table and §3.7/§3.8/§3.9 procedure steps aligned; invalidation propagators cannot acquire Experiment after starting at AV/LV/truth | §2.1, §3.6, §3.7, §3.8, §3.9 |
| C4 | Candidate cap: at most 100 active Draft entries / surviving frozen candidates; enforced at add and freeze; Gate 16 becomes `2 ≤ count ≤ 100`; §3.12 candidate embed is bounded by this cap | §2.3 Gate 16, §3.3, §3.12, §3.2 status table |


<!-- source-boundary: section4-final -->
# Section 4: Migration Strategy and Schema Evolution

## §4.1 Migration input schema and dependency

### Input head

This migration depends on `n1a2b3c4d5e6` (`n1_generation_assets_expand.py`), the sole Alembic head after #21. Its `down_revision` is `l1a2b3c4d5e6`.

```python
revision = "p1a2b3c4d5e6"
down_revision = "n1a2b3c4d5e6"
```

### Verified starting schema (experiment domain)

The following tables and columns exist at the `n1a2b3c4d5e6` head — this is the migration input, not `3d043c810a73`.

**`experiments`** — `id PK, workspace_id FK→workspaces(CASCADE), product_id FK→products(RESTRICT), name, state, revision, config_json, metric_contract_hash, frozen_snapshot_json(nullable), frozen_snapshot_hash(nullable UNIQUE), created_at, frozen_at(nullable), closed_at(nullable)`. Has `UNIQUE(workspace_id, id)`, composite FK `(workspace_id, product_id)→products`.

**`experiment_entries`** — `id PK, experiment_id FK→experiments(CASCADE), asset_version_id FK→asset_versions(RESTRICT), marketing_vars_json, position, created_at`. No `workspace_id`. UNIQUEs: `(experiment_id, position)`, `(experiment_id, asset_version_id)`.

**`experiment_ad_bindings`** — `id PK, experiment_entry_id FK→experiment_entries(CASCADE) NOT NULL, ad_account_id, ad_id, source, created_at`. No `workspace_id`. UNIQUEs: `(experiment_entry_id)`, `(ad_account_id, ad_id)` — global cross-workspace.

**`judgements`** — `id PK, experiment_id FK→experiments(CASCADE), algorithm_version, as_of, snapshot_hash, input_snapshot_json, result_json, created_at`. No `workspace_id`. UNIQUE: `(experiment_id, snapshot_hash, algorithm_version)`.

**`experiment_decisions`** — `id PK, experiment_id FK→experiments(CASCADE), judgement_id FK→judgements(CASCADE), experiment_entry_id FK→experiment_entries(CASCADE) NOT NULL, verdict, metric, estimate Numeric(24,12)(nullable), interval_low Numeric(24,12)(nullable), interval_high Numeric(24,12)(nullable), reason_codes_json, created_at`. No `workspace_id`. UNIQUE: `(judgement_id, experiment_entry_id)`.

**`experiment_state_events`** — `id PK, experiment_id FK→experiments(CASCADE), from_state(nullable), to_state, revision, actor_id, reason, created_at`. No `workspace_id`. UNIQUE: `(experiment_id, revision)`.

**`quality_reviews`** — has `id, workspace_id, asset_version_id, reviewer_id, review_type, review_value, ...`. Has `UNIQUE(workspace_id, id)`. No append-only trigger installed.

**`asset_versions`** — has `workspace_id, id, content_sha256, truth_package_id(nullable), origin_kind, execution_mode(nullable)` plus #21-added columns. Has `UNIQUE(workspace_id, id)`.

**`asset_qc_attempts`** — has `id, workspace_id, asset_version_id, run_item_id, feedback_round, qc_policy_version, rule_set_version, input_hash, provider_attempt_id(nullable), execution_mode, state, feedback_summary_json, revision, created_at, updated_at`. Has `UNIQUE(workspace_id, id)`.

**`asset_qc_check_results`** — has `id, workspace_id, qc_attempt_id, rule_id, rule_version, check_type, severity, outcome, evidence_json, feedback_detail, created_at`. Composite FK `(workspace_id, qc_attempt_id)→asset_qc_attempts`. Append-only trigger installed by #21: per-table function `reject_update_delete_asset_qc_check_results()` and trigger `trg_asset_qc_check_results_immutable`.

**`products`** — has `id, workspace_id, name, ...`. Has `UNIQUE(workspace_id, id)`.

**`library_entries`** — has `id, workspace_id, entry_type, ...`. Has `UNIQUE(workspace_id, id)`.

**`library_versions`** — has `id, workspace_id, entry_id, entry_type, version_no, content_json, content_hash, state, eligibility, ...`. Has `UNIQUE(workspace_id, id)`.

**`dimension_concept_keys`** — has `id, workspace_id(nullable), scope, name, ...`. Scope-aware.

**`run_items`** — has `id, workspace_id, generation_run_id, ...`. Has `UNIQUE(workspace_id, id)`.

### RLS/append-only input state

Experiment domain tables use parent-based RLS via CHILD_TABLES pattern. No existing experiment tables (`experiment_state_events`, `judgements`, `experiment_decisions`, `quality_reviews`) have append-only triggers. `asset_qc_check_results` has a per-table trigger from #21 (`reject_update_delete_asset_qc_check_results` / `trg_asset_qc_check_results_immutable`). This migration creates a new shared trigger function with erasure bypass (§4.9), replaces the #21 `asset_qc_check_results` trigger with the erasure-aware version, and installs triggers on both new immutable tables and existing evidence tables that lack them.

### Preflight orphan/duplicate checks

Before any schema changes, the migration runs validation queries and aborts if any fail:

```python
assert count("experiment_entries ee LEFT JOIN experiments e ON ee.experiment_id = e.id WHERE e.id IS NULL") == 0
assert count("experiment_ad_bindings ab LEFT JOIN experiment_entries ee ON ab.experiment_entry_id = ee.id WHERE ee.id IS NULL") == 0
assert count("experiment_decisions ed LEFT JOIN experiments e ON ed.experiment_id = e.id WHERE e.id IS NULL") == 0
assert count("judgements j LEFT JOIN experiments e ON j.experiment_id = e.id WHERE e.id IS NULL") == 0
```

---

## §4.2 New tables

Every new workspace-owned table declares `UNIQUE(workspace_id, id)` as its tenant anchor. Tables that also serve as lineage parents declare additional composite uniques as specified.

### `experiment_versions`

Immutable ExperimentVersionRecord. One per frozen Experiment. The canonical `frozen_snapshot_json` is the single source of evidence truth; all other columns are integrity projections verified against the snapshot at insert (fail-closed, no read-time fallback).

| Column | Type | Nullable | Constraint / Notes |
|---|---|---|---|
| `id` | String(64) | NO | PK |
| `workspace_id` | String(64) | NO | FK→workspaces(CASCADE); UNIQUE(workspace_id, id); UNIQUE(workspace_id, experiment_id, id) — lineage anchor |
| `experiment_id` | String(64) | NO | composite FK (workspace_id, experiment_id)→experiments(workspace_id, id) RESTRICT; UNIQUE(experiment_id) — one version per experiment |
| `canonicalization_version` | String(40) | NO | versioned canonical-JSON encoder |
| `frozen_snapshot_json` | JSON | NO | single source of evidence truth |
| `frozen_snapshot_hash` | String(64) | NO | SHA-256 of canonical JSON; INDEX (not UNIQUE) |
| `metric_contract_hash` | String(64) | NO | integrity projection of experiments.metric_contract_hash at freeze |
| `market` | String(10) | NO | ISO 3166-1 alpha-2 |
| `placement` | String(40) | NO | controlled vocabulary |
| `landing_page_evidence_hash` | String(64) | NO | |
| `audience_snapshot_json` | JSON | NO | |
| `audience_snapshot_hash` | String(64) | NO | server-recomputed |
| `daily_budget_amount` | Numeric(18,6) | NO | |
| `total_budget_cap` | Numeric(18,6) | NO | |
| `budget_currency` | String(3) | NO | ISO 4217 |
| `schedule_start_at` | DateTime(tz) | NO | |
| `schedule_end_at` | DateTime(tz) | NO | |
| `schedule_timezone` | String(100) | NO | IANA timezone |
| `attribution_click_window_days` | Integer | NO | 1–90 |
| `attribution_view_window_days` | Integer | NO | 1–90 |
| `statistical_plan_json` | JSON | NO | |
| `statistical_plan_hash` | String(64) | NO | server-recomputed |
| `marketing_vars_schema_version` | String(40) | NO | |
| `product_version_id` | String(64) | NO | composite FK (workspace_id, product_version_id)→library_versions(workspace_id, id) RESTRICT |
| `offer_version_id` | String(64) | NO | composite FK (workspace_id, offer_version_id)→library_versions(workspace_id, id) RESTRICT |
| `candidates_hash` | String(64) | NO | SHA-256 of ordered candidate hashes |
| `concept_resolutions_hash` | String(64) | NO | SHA-256 of resolution records |
| `created_at` | DateTime(tz) | NO | |

CHECKs: `attribution_click_window_days BETWEEN 1 AND 90`, `attribution_view_window_days BETWEEN 1 AND 90`, `daily_budget_amount > 0`, `total_budget_cap >= daily_budget_amount`, `schedule_end_at > schedule_start_at`.

### `experiment_version_candidates`

Immutable post-freeze identity per candidate.

| Column | Type | Nullable | Constraint / Notes |
|---|---|---|---|
| `id` | String(64) | NO | PK |
| `workspace_id` | String(64) | NO | UNIQUE(workspace_id, id) — tenant anchor; UNIQUE(workspace_id, experiment_version_id, id) — lineage anchor |
| `experiment_version_id` | String(64) | NO | composite FK (workspace_id, experiment_version_id)→experiment_versions(workspace_id, id) RESTRICT |
| `source_entry_id` | String(64) | YES | nullable composite FK (workspace_id, source_entry_id)→experiment_entries(workspace_id, id) RESTRICT |
| `source_kind` | String(32) | NO | CHECK IN ('generation', 'upload', 'reuse', 'manual') |
| `source_run_item_id` | String(64) | YES | nullable composite FK (workspace_id, source_run_item_id)→run_items(workspace_id, id) RESTRICT |
| `source_evidence_json` | JSON | YES | |
| `source_evidence_hash` | String(64) | YES | |
| `asset_version_id` | String(64) | NO | composite FK (workspace_id, asset_version_id)→asset_versions(workspace_id, id) RESTRICT |
| `asset_version_content_sha256` | String(64) | NO | copied from AssetVersion at freeze |
| `position` | Integer | NO | UNIQUE(experiment_version_id, position) |
| `marketing_vars_json` | JSON | NO | |
| `marketing_vars_hash` | String(64) | NO | |
| `execution_mode` | String(20) | NO | CHECK IN ('byok', 'sandbox', 'mock') |
| `eligibility_evidence_json` | JSON | NO | frozen §2.3 evidence |
| `eligibility_evidence_hash` | String(64) | NO | |
| `concepts_hash` | String(64) | NO | |
| `candidate_hash` | String(64) | NO | |
| `evaluated_at` | DateTime(tz) | NO | |
| `created_at` | DateTime(tz) | NO | |

UNIQUE(experiment_version_id, asset_version_id).

### `experiment_version_candidate_concepts`

Multi-concept per candidate. Open-ended dimension key (not an enum).

| Column | Type | Nullable | Constraint / Notes |
|---|---|---|---|
| `id` | String(64) | NO | PK |
| `workspace_id` | String(64) | NO | UNIQUE(workspace_id, id) |
| `experiment_version_id` | String(64) | NO | |
| `experiment_version_candidate_id` | String(64) | NO | composite FK (workspace_id, experiment_version_id, experiment_version_candidate_id)→experiment_version_candidates(workspace_id, experiment_version_id, id) RESTRICT |
| `dimension_key` | String(200) | NO | open-ended dimension identity |
| `stable_dimension_signature` | String(200) | NO | deterministic normalized signature |
| `concept_key_id` | String(64) | NO | simple FK→dimension_concept_keys.id RESTRICT (scope-aware exception) |
| `concept_key_scope` | String(20) | NO | CHECK IN ('workspace', 'platform') |
| `concept_key_name` | String(300) | NO | denormalized for snapshot |
| `value_json` | JSON | NO | |
| `value_hash` | String(64) | NO | |
| `evidence_json` | JSON | NO | |
| `duplicate_group_hash` | String(64) | NO | deterministic SHA-256 of (dimension_key, concept_key_id); always computed |
| `created_at` | DateTime(tz) | NO | |

UNIQUE(experiment_version_candidate_id, dimension_key).

### `experiment_version_concept_resolutions`

Immutable, created atomically with version. Excluded Draft candidates represented by stable source fingerprints, not frozen candidate IDs.

| Column | Type | Nullable | Constraint / Notes |
|---|---|---|---|
| `id` | String(64) | NO | PK |
| `workspace_id` | String(64) | NO | UNIQUE(workspace_id, id) |
| `experiment_version_id` | String(64) | NO | composite FK (workspace_id, experiment_version_id)→experiment_versions(workspace_id, id) RESTRICT |
| `duplicate_group_hash` | String(64) | NO | |
| `dimension_key` | String(200) | NO | |
| `concept_key_id` | String(64) | NO | FK→dimension_concept_keys.id RESTRICT |
| `source_candidate_fingerprints_json` | JSON | NO | |
| `action` | String(40) | NO | CHECK IN ('allow_controlled_duplicate', 'exclude_source_candidate', 'remap_before_freeze') |
| `outcome_evidence_json` | JSON | NO | |
| `reason` | Text | NO | |
| `actor_id` | String(100) | NO | |
| `authorization_evidence_json` | JSON | NO | |
| `resolved_at` | DateTime(tz) | NO | |
| `created_at` | DateTime(tz) | NO | |

UNIQUE(experiment_version_id, duplicate_group_hash).

### `decision_proposals`

Schema-2-only immutable header.

| Column | Type | Nullable | Constraint / Notes |
|---|---|---|---|
| `id` | String(64) | NO | PK |
| `workspace_id` | String(64) | NO | UNIQUE(workspace_id, id); UNIQUE(workspace_id, id, experiment_id, experiment_version_id, judgement_id) — full lineage anchor for Decision FK |
| `experiment_id` | String(64) | NO | composite FK (workspace_id, experiment_id)→experiments(workspace_id, id) RESTRICT |
| `judgement_id` | String(64) | NO | composite FK (workspace_id, experiment_id, experiment_version_id, judgement_id)→judgements(workspace_id, experiment_id, experiment_version_id, id) RESTRICT — 4-column FK enforces same experiment+version |
| `experiment_version_id` | String(64) | NO | composite FK (workspace_id, experiment_id, experiment_version_id)→experiment_versions(workspace_id, experiment_id, id) RESTRICT — 3-column FK enforces Version belongs to same experiment |
| `eligibility_context_hash` | String(64) | NO | §4.2.1 formula |
| `proposal_hash` | String(64) | NO | commits eligibility_context_hash + judgement hash + per-candidate verdicts |
| `proposed_entries_json` | JSON | NO | |
| `evidence_json` | JSON | NO | recheck evidence at proposal time |
| `created_at` | DateTime(tz) | NO | |

UNIQUE(workspace_id, judgement_id, eligibility_context_hash).

**Lineage enforcement on Proposal creation** — all lineage equalities are database-enforced via composite FKs:
- `Judgement.experiment_id == Proposal.experiment_id` — enforced by the 4-column FK `(workspace_id, experiment_id, experiment_version_id, judgement_id)→judgements(workspace_id, experiment_id, experiment_version_id, id)`.
- `Judgement.experiment_version_id == Proposal.experiment_version_id` — enforced by the same 4-column FK.
- `ExperimentVersion.experiment_id == Proposal.experiment_id` — enforced by the 3-column FK `(workspace_id, experiment_id, experiment_version_id)→experiment_versions(workspace_id, experiment_id, id)`.

### `decision_proposal_state_events`

Append-only lifecycle. `sequence` is CAS/event-stream authority.

| Column | Type | Nullable | Constraint / Notes |
|---|---|---|---|
| `id` | String(64) | NO | PK |
| `workspace_id` | String(64) | NO | UNIQUE(workspace_id, id) |
| `proposal_id` | String(64) | NO | composite FK (workspace_id, proposal_id)→decision_proposals(workspace_id, id) RESTRICT |
| `event_type` | String(32) | NO | CHECK IN ('created', 'consumed', 'invalidated', 'superseded') |
| `sequence` | Integer | NO | monotonic per proposal; CHECK(sequence >= 1) |
| `actor_id` | String(100) | YES | |
| `reason` | Text | YES | |
| `evidence_json` | JSON | YES | |
| `created_at` | DateTime(tz) | NO | |

UNIQUE(workspace_id, proposal_id, sequence).

### `decision_confirmations`

Gate between Proposal and material Decision. Lineage to Experiment/Version/Judgement derives through `proposal_id`; `experiment_id` is not stored (it would be an unconstrained duplicate).

| Column | Type | Nullable | Constraint / Notes |
|---|---|---|---|
| `id` | String(64) | NO | PK |
| `workspace_id` | String(64) | NO | UNIQUE(workspace_id, id); UNIQUE(workspace_id, id, proposal_id) — lineage anchor for Decision FK |
| `proposal_id` | String(64) | NO | composite FK (workspace_id, proposal_id)→decision_proposals(workspace_id, id) RESTRICT; UNIQUE(workspace_id, proposal_id) |
| `proposal_hash` | String(64) | NO | must match Proposal's proposal_hash |
| `expected_proposal_sequence` | Integer | NO | If-Match value |
| `idempotency_key_hash` | String(64) | NO | |
| `request_fingerprint_hash` | String(64) | NO | |
| `actor_id` | String(100) | NO | |
| `role` | String(32) | NO | |
| `authorization_epoch` | Integer | NO | |
| `authorization_evidence_json` | JSON | NO | |
| `confirmed_at` | DateTime(tz) | NO | |
| `created_at` | DateTime(tz) | NO | |

UNIQUE(workspace_id, idempotency_key_hash).

### `experiment_eligibility_events`

Append-only with idempotent deduplication (§2.5).

| Column | Type | Nullable | Constraint / Notes |
|---|---|---|---|
| `id` | String(64) | NO | PK |
| `workspace_id` | String(64) | NO | UNIQUE(workspace_id, id) |
| `experiment_id` | String(64) | NO | |
| `experiment_version_id` | String(64) | NO | composite FK (workspace_id, experiment_id, experiment_version_id)→experiment_versions(workspace_id, experiment_id, id) RESTRICT |
| `candidate_id` | String(64) | YES | when non-NULL: composite FK (workspace_id, experiment_version_id, candidate_id)→experiment_version_candidates(workspace_id, experiment_version_id, id) RESTRICT |
| `upstream_event_type` | String(40) | NO | CHECK IN ('review_rejected', 'qc_failed_blocking', 'product_version_withdrawn', 'offer_version_withdrawn', 'product_version_eligibility_changed', 'offer_version_eligibility_changed', 'stale_truth_detected', 'review_re_approved', 'qc_remediated') |
| `upstream_resource_type` | String(40) | NO | CHECK IN ('quality_review', 'asset_qc_attempt', 'library_version', 'truth_package', 'market_asset_binding') |
| `upstream_resource_id` | String(64) | NO | |
| `upstream_event_id` | String(64) | NO | |
| `previous_effective_eligibility` | String(20) | NO | CHECK IN ('eligible', 'ineligible', 'unknown') |
| `current_effective_eligibility` | String(20) | NO | CHECK IN ('eligible', 'ineligible') |
| `reversible` | Boolean | NO | |
| `evidence_json` | JSON | NO | |
| `observed_at` | DateTime(tz) | NO | §4.2.2 server-assigned monotonic |
| `created_at` | DateTime(tz) | NO | |

Partial unique dedup (both dialects):
- `UNIQUE(workspace_id, experiment_version_id, candidate_id, upstream_event_type, upstream_event_id) WHERE candidate_id IS NOT NULL`
- `UNIQUE(workspace_id, experiment_version_id, upstream_event_type, upstream_event_id) WHERE candidate_id IS NULL`

#### §4.2.2 `observed_at` append invariant

`observed_at` is server-assigned while holding the target projection's class-5 row lock. It must be strictly greater than the `observed_at` of the last folded event for the same projection stream (version-level or candidate-level). If the server clock returns a value ≤ the last folded event's `observed_at`, the service advances by the minimum representable tick (1 microsecond) to enforce strict monotonicity. The upstream source timestamp (when the triggering review/QC/truth event actually occurred) is preserved inside `evidence_json.upstream_observed_at` and is never used for ordering.

This guarantees `(observed_at ASC, id ASC)` order is identical whether events are folded incrementally or rebuilt from the full event set, because `observed_at` is strictly monotonic within each projection stream.

### `experiment_eligibility_projections`

Rebuildable current-eligibility projection. Stable sentinel rows created in the freeze transaction — never deleted, always updated in place.

| Column | Type | Nullable | Constraint / Notes |
|---|---|---|---|
| `id` | String(64) | NO | PK |
| `workspace_id` | String(64) | NO | UNIQUE(workspace_id, id) |
| `experiment_id` | String(64) | NO | |
| `experiment_version_id` | String(64) | NO | composite FK (workspace_id, experiment_id, experiment_version_id)→experiment_versions(workspace_id, experiment_id, id) RESTRICT |
| `level` | String(20) | NO | CHECK IN ('version', 'candidate') |
| `candidate_id` | String(64) | YES | when level='candidate': composite FK (workspace_id, experiment_version_id, candidate_id)→experiment_version_candidates(workspace_id, experiment_version_id, id) RESTRICT |
| `current_eligibility` | String(20) | NO | CHECK IN ('eligible', 'ineligible') |
| `irreversible_active` | Boolean | NO | |
| `head_event_id` | String(64) | YES | composite FK (workspace_id, head_event_id)→experiment_eligibility_events(workspace_id, id) RESTRICT when non-NULL |
| `head_hash` | String(64) | NO | non-NULL even at baseline |
| `head_sequence` | Integer | NO | 0 = baseline |
| `updated_at` | DateTime(tz) | NO | |

CHECK: `(level = 'version' AND candidate_id IS NULL) OR (level = 'candidate' AND candidate_id IS NOT NULL)`.

Partial uniques (both dialects):
- `UNIQUE(workspace_id, experiment_version_id) WHERE level = 'version'`
- `UNIQUE(workspace_id, experiment_version_id, candidate_id) WHERE level = 'candidate'`

**Baseline initialization** (freeze transaction): `head_hash = _sha256(_canonical_bytes(("baseline", level, entity_id)))` where `entity_id` is `experiment_version_id` for version-level or `candidate_id` for candidate-level. `head_event_id = NULL`, `head_sequence = 0`, `current_eligibility = 'eligible'`, `irreversible_active = false`.

**Fold formula**: `new_head_hash = _sha256(_canonical_bytes((prev_head_hash, event_id, current_effective_eligibility, reversible)))`.

**Event append**: under class-5 lock (`SELECT ... FOR UPDATE` on projection row), server assigns `observed_at` (§4.2.2), appends event, computes new `head_hash` via fold formula, and `UPDATE ... SET head_event_id = :eid, head_hash = :h, head_sequence = head_sequence + 1, current_eligibility = :e, irreversible_active = :i, updated_at = :now WHERE id = :pid AND head_sequence = :expected`. CAS on `head_sequence` is defense-in-depth.

**In-place rebuild**: lock rows, reset to baseline, replay events in `(observed_at ASC, id ASC)` order via UPDATE. No DELETE. Deterministic: the `observed_at` monotonicity invariant guarantees fold order = rebuild order, so `head_hash` after rebuild equals `head_hash` after incremental folding.

#### §4.2.1 `eligibility_context_hash` canonical formula

```python
eligibility_context_hash = _sha256(_canonical_bytes((
    frozen_snapshot_hash,                          # input 1: ExperimentVersion
    version_projection_head_hash,                  # input 2: version-level projection
    tuple(sorted(                                  # input 3: candidate projections
        ((c.candidate_id, c.head_hash) for c in candidate_projections),
        key=lambda x: x[0]
    )),
    recheck_evidence_hash                          # input 4: post-freeze recheck
)))
```

All four inputs from §3.8:
1. `ExperimentVersion.frozen_snapshot_hash`
2. Current version-level eligibility projection `head_hash`
3. Canonical ordered set of `(candidate_id, candidate_head_hash)` tuples, sorted by `candidate_id`
4. Canonical hash of the authoritative post-freeze recheck evidence computed under the same locks

The candidate ID travels with each head hash so the set is not positionally ambiguous. Canonical sorting means the input enumeration order is irrelevant — only set membership matters. This formula is used identically for `decision_proposals.eligibility_context_hash`, Proposal uniqueness, Proposal evidence, and acceptance test vectors.

`proposal_hash` commits: `_sha256(_canonical_bytes((eligibility_context_hash, judgement_snapshot_hash, proposed_entries_canonical)))`.

### `contextual_verdict_projections`

Rebuildable current-verdict read projection (§1.10). One row per unique experiment context.

| Column | Type | Nullable | Constraint / Notes |
|---|---|---|---|
| `id` | String(64) | NO | PK |
| `workspace_id` | String(64) | NO | UNIQUE(workspace_id, id) |
| `experiment_id` | String(64) | NO | composite FK (workspace_id, experiment_id)→experiments(workspace_id, id) RESTRICT |
| `experiment_version_id` | String(64) | NO | composite FK (workspace_id, experiment_version_id)→experiment_versions(workspace_id, id) RESTRICT |
| `candidate_id` | String(64) | NO | composite FK (workspace_id, candidate_id)→experiment_version_candidates(workspace_id, id) RESTRICT |
| `decision_id` | String(64) | NO | composite FK (workspace_id, decision_id)→experiment_decisions(workspace_id, id) RESTRICT |
| `confirmation_id` | String(64) | NO | composite FK (workspace_id, confirmation_id)→decision_confirmations(workspace_id, id) RESTRICT |
| `verdict` | String(32) | NO | CHECK IN ('win', 'learn', 'lose') |
| `metric` | String(20) | NO | |
| `estimate` | Numeric(24,12) | YES | |
| `asset_version_id` | String(64) | NO | composite FK (workspace_id, asset_version_id)→asset_versions(workspace_id, id) RESTRICT |
| `product_version_id` | String(64) | NO | composite FK (workspace_id, product_version_id)→library_versions(workspace_id, id) RESTRICT |
| `offer_version_id` | String(64) | NO | composite FK (workspace_id, offer_version_id)→library_versions(workspace_id, id) RESTRICT |
| `market` | String(10) | NO | |
| `placement` | String(40) | NO | |
| `landing_page_evidence_hash` | String(64) | NO | |
| `confirmed_at` | DateTime(tz) | NO | |
| `updated_at` | DateTime(tz) | NO | |
| `created_at` | DateTime(tz) | NO | |

Current-key unique: `UNIQUE(workspace_id, experiment_id, asset_version_id, offer_version_id, market, placement, landing_page_evidence_hash)`.

Rebuild/upsert ordering: `(confirmed_at DESC, decision_id DESC)`. On invalidation, the obsolete row is removed by the projection-maintenance function (§4.9) and replaced by the next-most-recent valid Decision if one exists.

INDEX: `(workspace_id, asset_version_id, confirmed_at DESC)`.

**Single-transaction lineage validation** on projection write — all fields are copied from the validated material Decision chain:
- `asset_version_id == Candidate.asset_version_id` — the candidate's frozen asset identity.
- `product_version_id == ExperimentVersion.product_version_id` — from the frozen Version.
- `offer_version_id == ExperimentVersion.offer_version_id` — from the frozen Version.
- `market == ExperimentVersion.market`, `placement == ExperimentVersion.placement`, `landing_page_evidence_hash == ExperimentVersion.landing_page_evidence_hash` — frozen context fields.
- `candidate_id` belongs to `experiment_version_id` (validated via candidate's parent version).
- `confirmation_id` belongs to Decision's `proposal_id` (validated via confirmation's parent proposal).
- `experiment_version_id == Decision.experiment_version_id`, `experiment_id == Decision.experiment_id`.

Negative tests verify that combining independently valid IDs from different versions is rejected.

---

## §4.3 Column additions and evolution of existing tables

### `products` table — ProductProfile identity bridge

| Column | Type | Default | Constraint | Backfill |
|---|---|---|---|---|
| `product_profile_entry_id` | String(64) | NULL | composite FK (workspace_id, product_profile_entry_id)→library_entries(workspace_id, id) RESTRICT | NULL for all existing rows |

Set-once: enforced by PostgreSQL column trigger (§4.9) and SQLite service guard. No guessed backfill.

Partial unique: `UNIQUE(workspace_id, product_profile_entry_id) WHERE product_profile_entry_id IS NOT NULL` (both dialects).

### `experiments` table

| Column | Type | Default | Constraint | Backfill |
|---|---|---|---|---|
| `record_schema_version` | Integer | NULL | CHECK(record_schema_version IS NULL OR record_schema_version = 2) | NULL for existing rows |
| `started_at` | DateTime(tz) | NULL | — | NULL for all existing rows |

`record_schema_version`: the server writes `2` on INSERT for schema-2 Experiments. Every UPDATE that changes `record_schema_version` — including `NULL→2` — is rejected by the column trigger (§4.9). A legacy NULL row cannot be promoted to schema 2 via UPDATE; it stays legacy permanently.

`started_at`: set-once at `start_experiment()`. Column trigger (§4.9) prevents change after initial non-NULL write.

`frozen_snapshot_hash` UNIQUE→non-unique INDEX.

`config_json` remains the single mutable Draft context authority (§3.3).

### `experiment_entries` table

| Column | Type | Default | Constraint | Backfill |
|---|---|---|---|---|
| `workspace_id` | String(64) | — | FK→workspaces(CASCADE) | from parent experiments.workspace_id |

After backfill: NOT NULL. Add `UNIQUE(workspace_id, id)`, composite FK `(workspace_id, experiment_id)→experiments(workspace_id, id)` RESTRICT, composite FK `(workspace_id, asset_version_id)→asset_versions(workspace_id, id)` RESTRICT.

### `experiment_ad_bindings` table

| Column | Type | Default | Constraint | Backfill |
|---|---|---|---|---|
| `workspace_id` | String(64) | — | — | from grandparent |
| `experiment_version_candidate_id` | String(64) | NULL | — | NULL for legacy |
| `record_schema_version` | Integer | NULL | CHECK(IS NULL OR = 2) | NULL for existing |

After backfill: `workspace_id` NOT NULL.

Changes: ALTER `experiment_entry_id` nullable. Drop global `UNIQUE(ad_account_id, ad_id)`, drop `UNIQUE(experiment_entry_id)`. Add `UNIQUE(workspace_id, id)`, `UNIQUE(workspace_id, ad_account_id, ad_id)`. Composite FK `(workspace_id, experiment_version_candidate_id)→experiment_version_candidates(workspace_id, id)` RESTRICT. Composite FK `(workspace_id, experiment_entry_id)→experiment_entries(workspace_id, id)` RESTRICT.

Partial uniques: legacy `UNIQUE(workspace_id, experiment_entry_id) WHERE record_schema_version IS NULL`; schema-2 `UNIQUE(workspace_id, experiment_version_candidate_id) WHERE record_schema_version = 2`.

Bidirectional CHECK: `CASE WHEN record_schema_version IS NULL THEN experiment_entry_id IS NOT NULL WHEN record_schema_version = 2 THEN experiment_entry_id IS NULL AND experiment_version_candidate_id IS NOT NULL ELSE FALSE END`.

`record_schema_version`: the server writes the value on INSERT; every UPDATE that changes it is rejected by the column trigger (§4.9). A legacy NULL row cannot be promoted to schema-2 via UPDATE, and a schema-2 row cannot revert. This prevents switching between CASE branches after creation.

### `judgements` table

| Column | Type | Default | Constraint | Backfill |
|---|---|---|---|---|
| `workspace_id` | String(64) | — | — | from parent experiments.workspace_id |
| `experiment_version_id` | String(64) | NULL | — | NULL for legacy |
| `record_schema_version` | Integer | NULL | — | NULL for existing |

After backfill: `workspace_id` NOT NULL. Add `UNIQUE(workspace_id, id)`, `UNIQUE(workspace_id, experiment_id, experiment_version_id, id)` — 4-column lineage anchor for Proposal FK. Composite FK `(workspace_id, experiment_id)→experiments(workspace_id, id)` RESTRICT. Three-column same-experiment Version FK: `(workspace_id, experiment_id, experiment_version_id)→experiment_versions(workspace_id, experiment_id, id)` RESTRICT. `UNIQUE(workspace_id, experiment_id, snapshot_hash, algorithm_version)`.

Bidirectional CHECK: `CASE WHEN record_schema_version IS NULL THEN experiment_version_id IS NULL WHEN record_schema_version = 2 THEN experiment_version_id IS NOT NULL ELSE FALSE END`. CHECK `(record_schema_version IS NULL OR record_schema_version = 2)`.

### `experiment_decisions` table

| Column | Type | Default | Constraint | Backfill |
|---|---|---|---|---|
| `workspace_id` | String(64) | — | — | from parent experiments.workspace_id |
| `experiment_version_id` | String(64) | NULL | — | NULL for legacy |
| `experiment_version_candidate_id` | String(64) | NULL | — | NULL for legacy |
| `proposal_id` | String(64) | NULL | — | NULL for legacy |
| `confirmation_id` | String(64) | NULL | — | NULL for legacy |
| `actor_id` | String(100) | NULL | — | NULL for legacy |
| `confirmed_at` | DateTime(tz) | NULL | — | NULL for legacy |
| `context_hash` | String(64) | NULL | — | NULL for legacy |
| `decision_schema_version` | Integer | NULL | — | NULL for existing |

After backfill: `workspace_id` NOT NULL. ALTER `experiment_entry_id` nullable. Add `UNIQUE(workspace_id, id)`.

**Database-enforced composite FKs:**
- `(workspace_id, experiment_id)→experiments(workspace_id, id)` RESTRICT
- `(workspace_id, judgement_id)→judgements(workspace_id, id)` RESTRICT
- `(workspace_id, experiment_version_id)→experiment_versions(workspace_id, id)` RESTRICT
- `(workspace_id, experiment_version_id, experiment_version_candidate_id)→experiment_version_candidates(workspace_id, experiment_version_id, id)` RESTRICT — 3-column FK enforces candidate belongs to version
- `(workspace_id, proposal_id, experiment_id, experiment_version_id, judgement_id)→decision_proposals(workspace_id, id, experiment_id, experiment_version_id, judgement_id)` RESTRICT — 5-column FK enforces Decision.experiment_id, experiment_version_id, and judgement_id all match Proposal's
- `(workspace_id, confirmation_id, proposal_id)→decision_confirmations(workspace_id, id, proposal_id)` RESTRICT — 3-column FK enforces confirmation belongs to same proposal

Partial uniques: legacy `UNIQUE(judgement_id, experiment_entry_id) WHERE decision_schema_version IS NULL`; schema-2 `UNIQUE(workspace_id, confirmation_id, experiment_version_candidate_id) WHERE decision_schema_version = 2`.

Full bidirectional CASE CHECK (§1.8): `CASE WHEN decision_schema_version IS NULL THEN experiment_entry_id IS NOT NULL AND experiment_version_id IS NULL AND experiment_version_candidate_id IS NULL AND proposal_id IS NULL AND confirmation_id IS NULL AND actor_id IS NULL AND confirmed_at IS NULL AND context_hash IS NULL WHEN decision_schema_version = 2 THEN experiment_entry_id IS NULL AND experiment_version_id IS NOT NULL AND experiment_version_candidate_id IS NOT NULL AND proposal_id IS NOT NULL AND confirmation_id IS NOT NULL AND actor_id IS NOT NULL AND confirmed_at IS NOT NULL AND context_hash IS NOT NULL ELSE FALSE END`. CHECK `(decision_schema_version IS NULL OR decision_schema_version = 2)`.

**Single-transaction insertion integrity checks** (residual cross-row equalities not expressible as database constraints, validated under the Experiment aggregate lock):
1. `actor_id` must equal `Confirmation.actor_id` — copied field, not an FK.
2. `confirmed_at` must equal `Confirmation.confirmed_at` — copied field, not an FK.
3. `context_hash` = `_sha256(_canonical_bytes((experiment_id, offer_version_id, market, placement, landing_page_evidence_hash)))` from the frozen ExperimentVersion — derived value.

All lineage equalities are now database-enforced:
- `experiment_id == Proposal.experiment_id` — via the 5-column FK to `decision_proposals`.
- `experiment_version_id == Proposal.experiment_version_id` — via the 5-column FK to `decision_proposals`.
- `judgement_id == Proposal.judgement_id` — via the 5-column FK to `decision_proposals`.
- `Judgement.experiment_version_id == Decision.experiment_version_id` — transitively enforced: the 5-column Decision→Proposal FK ensures `Decision.experiment_version_id == Proposal.experiment_version_id`, and the 4-column Proposal→Judgement FK ensures `Proposal.experiment_version_id == Judgement.experiment_version_id`.
- Candidate→Version: `experiment_version_candidate_id` belongs to `experiment_version_id` — via the 3-column FK to `experiment_version_candidates`.
- Confirmation→Proposal: `confirmation_id` references the same Proposal as `proposal_id` — via the 3-column FK to `decision_confirmations`.

### `experiment_state_events` table

| Column | Type | Default | Constraint | Backfill |
|---|---|---|---|---|
| `workspace_id` | String(64) | — | — | from parent experiments.workspace_id |
| `evidence_json` | JSON | NULL | — | NULL for existing |

After backfill: `workspace_id` NOT NULL. Add `UNIQUE(workspace_id, id)`, composite FK `(workspace_id, experiment_id)→experiments(workspace_id, id)` RESTRICT.

### `asset_versions` table — truth-dependency columns

| Column | Type | Default | Constraint | Backfill |
|---|---|---|---|---|
| `truth_dependency_expected` | Boolean | NULL | — | §4.4 |
| `truth_dependency_policy_version` | String(40) | NULL | — | §4.4 |
| `truth_dependency_evidence_json` | JSON | NULL | — | §4.4 |
| `truth_dependency_evidence_hash` | String(64) | NULL | — | §4.4 |

CHECKs (exact §2.4):
1. `CHECK(truth_dependency_expected IS NOT FALSE OR truth_package_id IS NULL)`
2. `CHECK(truth_package_id IS NULL OR truth_dependency_expected = true)`
3. `CHECK(truth_dependency_expected IS NULL OR (truth_dependency_policy_version IS NOT NULL AND truth_dependency_evidence_hash IS NOT NULL))`

Write-once enforcement: column trigger (§4.9) requires atomic group initialization and rejects `v1-bootstrap` on post-cutover writes.

### `asset_qc_attempts` table

| Column | Type | Default | Constraint | Backfill |
|---|---|---|---|---|
| `attempt_sequence` | Integer | NULL | — | §4.5 |
| `terminal_at` | DateTime(tz) | NULL | — | §4.5 |
| `remediates_attempt_id` | String(64) | NULL | composite FK (workspace_id, remediates_attempt_id)→asset_qc_attempts(workspace_id, id) RESTRICT | NULL for existing |

After backfill: `attempt_sequence` NOT NULL. Add `UNIQUE(workspace_id, asset_version_id, attempt_sequence)`, partial `UNIQUE(workspace_id, asset_version_id) WHERE state IN ('pending', 'running')`.

Write-once trigger on `attempt_sequence`, `remediates_attempt_id` (§4.9).

**Remediation invariants** (service enforcement, tested): same AssetVersion; referenced attempt `failed_blocking`; lower sequence; no cycle; policy coverage; `max_qc_attempts_per_asset_version` cap.

### `asset_qc_check_results` table

| Column | Type | Default | Constraint | Backfill |
|---|---|---|---|---|
| `evidence_hash` | String(64) | NULL | — | §4.5 |

After backfill: NOT NULL.

---

## §4.4 Truth-dependency backfill (§2.4 grandfathered migration)

The backfill runs inside the migration transaction. `truth_dependency_evidence_json` is stored as a structured Python dict bound through a typed SQLAlchemy `JSON` column expression — no `CAST`. The hash is computed over the canonical bytes of the hash tuple, not over the JSON string.

```python
from sqlalchemy import column as sa_col, table as sa_table, JSON as SA_JSON
_av = sa_table("asset_versions",
    sa_col("id"), sa_col("truth_package_id"),
    sa_col("truth_dependency_expected"), sa_col("truth_dependency_policy_version"),
    sa_col("truth_dependency_evidence_json", SA_JSON),
    sa_col("truth_dependency_evidence_hash"))
```

**Pass 1 — `truth_package_id IS NULL` → expected=false:**

```python
evidence_obj = {"migration_reason": "grandfathered_pre_truth_system", "original_truth_package_id": None}
evidence_hash = _sha256(_canonical_bytes(("v1-bootstrap", False, None, "grandfathered_pre_truth_system")))
conn.execute(
    _av.update()
    .where(_av.c.truth_package_id.is_(None))
    .where(_av.c.truth_dependency_expected.is_(None))
    .values(truth_dependency_expected=False, truth_dependency_policy_version="v1-bootstrap",
            truth_dependency_evidence_json=evidence_obj, truth_dependency_evidence_hash=evidence_hash))
```

**Pass 2 — `truth_package_id IS NOT NULL` → expected=true:**

```python
rows = conn.execute(text(
    "SELECT id, truth_package_id FROM asset_versions "
    "WHERE truth_package_id IS NOT NULL AND truth_dependency_expected IS NULL"))
for row in rows:
    evidence_obj = {"migration_reason": "grandfathered_pre_truth_system",
                    "original_truth_package_id": row.truth_package_id}
    evidence_hash = _sha256(_canonical_bytes(("v1-bootstrap", True, row.truth_package_id, "grandfathered_pre_truth_system")))
    conn.execute(
        _av.update().where(_av.c.id == row.id).where(_av.c.truth_dependency_expected.is_(None))
        .values(truth_dependency_expected=True, truth_dependency_policy_version="v1-bootstrap",
                truth_dependency_evidence_json=evidence_obj, truth_dependency_evidence_hash=evidence_hash))
```

After this migration, the write-once trigger rejects `v1-bootstrap` as a `truth_dependency_policy_version` on any new INSERT or later initialization, making it provably migration-only.

---

## §4.5 QC attempt sequence and evidence hash backfill

### Attempt sequence

**PostgreSQL:**
```sql
WITH sequenced AS (
    SELECT id, ROW_NUMBER() OVER (
        PARTITION BY workspace_id, asset_version_id ORDER BY created_at ASC, id ASC
    ) AS seq FROM asset_qc_attempts WHERE attempt_sequence IS NULL
)
UPDATE asset_qc_attempts SET attempt_sequence = sequenced.seq
FROM sequenced WHERE asset_qc_attempts.id = sequenced.id;
```

**SQLite:** Python-side loop with partition tracking (§4.7 encoder).

### Terminal-at backfill

```sql
UPDATE asset_qc_attempts SET terminal_at = updated_at
WHERE state IN ('passed', 'failed_blocking', 'failed_nonblocking', 'uncertain') AND terminal_at IS NULL;
```

### Evidence hash backfill

Each `asset_qc_check_results` row is read through a typed SQLAlchemy `JSON` column expression so the `evidence_json` column returns a structured Python `dict` on both dialects (not a raw JSON string on SQLite). The structured object is normalized and hashed directly inside the canonical tuple — no intermediate `_canonical_json()` string wrapping:

```python
_qc = sa_table("asset_qc_check_results",
    sa_col("id"), sa_col("rule_id"), sa_col("rule_version"),
    sa_col("check_type"), sa_col("severity"), sa_col("outcome"),
    sa_col("evidence_json", SA_JSON), sa_col("evidence_hash"))
rows = conn.execute(
    _qc.select().where(_qc.c.evidence_hash.is_(None)))
for row in rows:
    evidence_obj = _normalize_legacy_numerics(row.evidence_json)
    h = _sha256(_canonical_bytes((row.rule_id, row.rule_version, row.check_type,
                                   row.severity, row.outcome, evidence_obj)))
    conn.execute(
        _qc.update().where(_qc.c.id == row.id)
        .values(evidence_hash=h))
```

`_normalize_legacy_numerics` (§4.7) handles legacy floats from the database driver.

---

## §4.6 Workspace-id backfill for experiment child tables

Five tables gain `workspace_id` from their parent experiment via correlated UPDATE subquery. After backfill, all five set NOT NULL.

---

## §4.7 Canonical hash routine

Migration-local canonical encoder. Frozen in this revision; future app-code changes cannot alter historical migration hashes.

```python
import hashlib, json, decimal, datetime

_MIGRATION_CANON_VERSION = "p1-migration-v1"

def _reject_floats(obj):
    """Recursively walk and reject any binary float or non-string dict key in canonical input."""
    if isinstance(obj, float):
        raise TypeError(f"Binary float not allowed in canonical encoding: {obj!r}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"Non-string dict key not allowed in canonical encoding: {k!r}")
            _reject_floats(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _reject_floats(v)

def _canonical_decimal_str(d: decimal.Decimal) -> str:
    """Deterministic plain-notation string: no exponent, no trailing fractional zeros, negative zero collapsed."""
    d = d.normalize()
    if d.is_zero():
        return "0"
    s = format(d, 'f')
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s

def _canonical_json(obj: object) -> str:
    _reject_floats(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=_json_default, allow_nan=False)

def _json_default(o):
    if isinstance(o, decimal.Decimal):
        if not o.is_finite():
            raise ValueError(f"Non-finite Decimal: {o}")
        return _canonical_decimal_str(o)
    if isinstance(o, (datetime.datetime,)):
        if o.tzinfo is None:
            raise ValueError(f"Naive datetime not allowed: {o}")
        utc_dt = o.astimezone(datetime.timezone.utc)
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    if isinstance(o, tuple):
        return list(o)
    raise TypeError(f"Not serializable: {type(o)}")

def _canonical_bytes(tup: tuple) -> bytes:
    return _canonical_json(list(tup)).encode("utf-8")

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _normalize_legacy_numerics(obj):
    """Migration-only: recursively convert database-driver floats to Decimal for deterministic hashing."""
    if isinstance(obj, float):
        return decimal.Decimal(repr(obj))
    if isinstance(obj, dict):
        return {k: _normalize_legacy_numerics(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_legacy_numerics(v) for v in obj]
    return obj
```

Key properties:
- **Binary floats rejected by `_canonical_json` itself**: `_reject_floats` recursively walks the input tree *before* `json.dumps`. Python's JSON encoder serializes `float` natively without calling `default=`, so the `_json_default` handler alone cannot intercept them. `_reject_floats` closes this gap: any `float` instance in the canonical input raises `TypeError`. The migration backfill uses `_normalize_legacy_numerics` to convert legacy database-driver floats to `Decimal` before canonicalization — it is the explicit exception.
- **Dict keys must be strings**: `_reject_floats` also rejects non-string dict keys (integer, float, or other types) instead of allowing `json.dumps` to silently coerce them to strings.
- **Decimal canonical form**: `_canonical_decimal_str` normalizes, collapses negative zero to `"0"`, and emits deterministic fixed-point notation without trailing fractional zeros or exponent ambiguity (`Decimal("1.230000") → "1.23"`, `Decimal("1000") → "1000"` not `"1E+3"`, `Decimal("-0") → "0"`). Budget fields enter as field-validated fixed-6dp strings; generic evidence values in `Numeric(24,12)` retain their validated precision. The encoder never rounds.
- **Microsecond precision preserved**: `%f` emits 6 digits (microseconds). No truncation to milliseconds. The canonicalization version `p1-migration-v1` declares this precision.
- **Timezone-aware required**: naive datetimes raise. `astimezone(UTC)` normalizes offset-aware values.
- **Tuples**: converted to lists for JSON serialization. Nested tuples in `_canonical_bytes` input are handled by `_json_default`.

---

## §4.8 Migration execution order (authoritative)

One migration revision, one authoritative order. Individual backfill UPDATEs are idempotent; overall DDL is not blindly re-runnable (§4.10).

### Step 1: Preflight assertions
Assert `n1a2b3c4d5e6` head. Run orphan/duplicate checks (§4.1). Abort on failure.

### Step 2: Add nullable columns
All columns added nullable, no constraint changes. (Full list: §4.3 tables.)

### Step 3: Run backfills and verify completeness
Workspace-id (§4.6), truth-dependency (§4.4), QC sequence/terminal/evidence-hash (§4.5). Verify all `IS NULL` counts = 0. Abort on failure.

### Step 4: Set NOT NULL and create parent composite unique anchors
NOT NULL on: `experiment_entries.workspace_id`, `experiment_state_events.workspace_id`, `experiment_ad_bindings.workspace_id`, `judgements.workspace_id`, `experiment_decisions.workspace_id`, `asset_qc_attempts.attempt_sequence`, `asset_qc_check_results.evidence_hash`.

Parent anchors: `UNIQUE(workspace_id, id)` on `experiment_entries`, `judgements`, `experiment_state_events`, `experiment_ad_bindings`, `experiment_decisions`. Additional lineage anchors: `UNIQUE(workspace_id, experiment_id, experiment_version_id, id)` on `judgements` (required before Proposal creation in step 5).

### Step 5: Create new tables in topological order
1. `experiment_versions` — needs experiments, library_versions ✓.
2. `experiment_version_candidates` — needs versions(w,id) ✓, entries(w,id) ✓, asset_versions(w,id) ✓, run_items(w,id) ✓.
3. `experiment_version_candidate_concepts` — needs candidates(w,ev,id) ✓.
4. `experiment_version_concept_resolutions` — needs versions(w,id) ✓.
5. `decision_proposals` — needs experiments(w,id) ✓, judgements(w,exp,ev,id) ✓, versions(w,exp,id) ✓. Declares `UNIQUE(workspace_id, id, experiment_id, experiment_version_id, judgement_id)`.
6. `decision_proposal_state_events` — needs proposals(w,id) ✓.
7. `decision_confirmations` — needs proposals(w,id) ✓. Declares `UNIQUE(workspace_id, id, proposal_id)`.
8. `experiment_eligibility_events` — needs versions(w,exp,id) ✓, candidates(w,ev,id) ✓.
9. `experiment_eligibility_projections` — needs versions(w,exp,id) ✓, candidates(w,ev,id) ✓, events(w,id) ✓.
10. `contextual_verdict_projections` — needs all parent anchors ✓.

### Step 6: Deferred FKs, CHECKs, partial uniques
Drop/replace existing constraints on experiments, bindings, judgements, decisions. Add all composite FKs: Decision→Proposal 5-column lineage FK, Decision→Candidate 3-column lineage FK, Decision→Confirmation 3-column lineage FK, Proposal→Judgement 4-column lineage FK, Proposal→Version 3-column lineage FK. Add bidirectional CHECKs, partial uniques on both dialects.

### Step 7: Immutability enforcement, RLS, grants, indexes
Create shared erasure-aware trigger function, replace #21's `asset_qc_check_results` per-table trigger, install all append-only triggers, column write-once triggers, erasure procedure, projection-maintenance function, RLS policies, role grants, performance indexes (§4.9).

### Step 8: Post-migration integrity queries
§4.11 acceptance checks.

---

## §4.9 RLS, immutability enforcement, controlled erasure, and SQLite compatibility

### RLS (PostgreSQL)

```python
NEW_WORKSPACE_OWNED = (
    "experiment_versions", "experiment_version_candidates",
    "experiment_version_candidate_concepts", "experiment_version_concept_resolutions",
    "decision_proposals", "decision_proposal_state_events", "decision_confirmations",
    "experiment_eligibility_events", "experiment_eligibility_projections",
    "contextual_verdict_projections",
)
```

Each: `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, `CREATE POLICY tenant_isolation ... USING/WITH CHECK (marketai_context_is_current(workspace_id))`.

### Role/grant matrix

| Role | Immutable tables | Eligibility projections | Verdict projections | Existing experiment tables |
|---|---|---|---|---|
| `marketai_domain_api` | SELECT, INSERT | SELECT, INSERT, UPDATE | SELECT, INSERT, UPDATE | Unchanged |
| `marketai_domain_worker` | SELECT, INSERT | SELECT, INSERT, UPDATE | SELECT, INSERT, UPDATE | Unchanged |
| `marketai_projection_writer` | SELECT | SELECT, UPDATE | SELECT, INSERT, UPDATE, DELETE | SELECT |
| `marketai_erasure_principal` | ALL (workspace-scoped) | ALL (workspace-scoped) | ALL (workspace-scoped) | ALL (workspace-scoped) |
| `marketai_domain_readonly` | SELECT | SELECT | SELECT | Unchanged |

- **Immutable tables**: INSERT-only. No UPDATE/DELETE grant.
- **Eligibility projections**: INSERT + UPDATE. No DELETE grant — sentinel stability.
- **Verdict projections**: general API/worker roles get INSERT + UPDATE only (no DELETE). The `marketai_projection_writer` role gets SELECT, INSERT, UPDATE, DELETE — the full set required by the projection-maintenance function's dependency chain (SELECT on source tables to find the next valid Decision, INSERT for the replacement row, UPDATE for projection fields, DELETE for the obsolete row). `marketai_projection_writer` also holds SELECT on immutable and existing experiment tables (to read the Decision/Confirmation/Candidate/Version chain) and SELECT + UPDATE on eligibility projections (to acquire the class-5 `SELECT ... FOR UPDATE` row lock — PostgreSQL requires UPDATE privilege for row-level locks). Ordinary API/worker SQL DELETE on verdict projections is denied by lack of grant.
- **`marketai_erasure_principal`**: privileged role used exclusively by the controlled Workspace erasure procedure (below). Not granted to general API/worker connections. `marketai_domain_api`, `marketai_domain_worker`, and `marketai_projection_writer` are not members of `marketai_erasure_principal`.

### Append-only triggers (PostgreSQL)

This migration creates a new shared trigger function `marketai_reject_update_or_delete()` with the controlled-erasure bypass. It is distinct from #21's per-table functions (`reject_update_delete_{table}()`).

For `asset_qc_check_results`, this migration replaces the existing #21 trigger:
1. Drop `trg_asset_qc_check_results_immutable` trigger.
2. Drop `reject_update_delete_asset_qc_check_results()` function.
3. Install new trigger using the shared erasure-aware function.

On downgrade, the original #21 per-table function and trigger are restored.

New triggers use ordinary one-shot `CREATE TRIGGER` with catalog preflight qualified by relation:

```python
def _create_append_only_trigger(conn, table_name):
    trigger_name = f"trg_{table_name}_append_only"
    exists = conn.execute(text(
        "SELECT 1 FROM pg_trigger "
        "WHERE tgname = :name AND tgrelid = to_regclass(:table)"
    ), {"name": trigger_name, "table": table_name}).scalar()
    if exists:
        return
    conn.execute(text(f"""
        CREATE TRIGGER {trigger_name}
        BEFORE UPDATE OR DELETE ON {table_name}
        FOR EACH ROW EXECUTE FUNCTION marketai_reject_update_or_delete()
    """))
```

```python
APPEND_ONLY_TABLES = (
    "experiment_versions", "experiment_version_candidates",
    "experiment_version_candidate_concepts", "experiment_version_concept_resolutions",
    "decision_proposals", "decision_proposal_state_events", "decision_confirmations",
    "experiment_eligibility_events",
    "quality_reviews", "experiment_state_events", "judgements", "experiment_decisions",
    "asset_qc_check_results",
)
```

`asset_qc_check_results` is included because its #21 trigger is replaced with the erasure-aware version.

**Eligibility projections**: `trg_experiment_eligibility_projections_no_delete` — BEFORE DELETE fires rejection (with erasure bypass). UPDATE allowed for event folding/rebuild.

**Verdict projections**: no append-only trigger. DELETE restricted to `marketai_projection_writer` role (grant-based, not trigger-based). All deletes go through the projection-maintenance function (below).

### Projection-maintenance function (PostgreSQL)

`SECURITY DEFINER` function `marketai_maintain_verdict_projection()` owned by `marketai_projection_writer`, with `SET search_path = ''`, `REVOKE ALL ON FUNCTION ... FROM PUBLIC`, and `GRANT EXECUTE ON FUNCTION ... TO marketai_domain_api, marketai_domain_worker`. The `marketai_domain_api` and `marketai_domain_worker` roles are NOT members of `marketai_projection_writer`; they can only invoke this function, not execute arbitrary SQL as the writer role.

The function accepts `(p_workspace_id, p_experiment_id, p_asset_version_id, p_offer_version_id, p_market, p_placement, p_landing_page_evidence_hash)` — the exact current-key tuple. It:
1. Validates the caller's tenant context via `marketai_context_is_current(p_workspace_id)`.
2. Acquires the class-5 aggregate/sentinel lock for the experiment.
3. Deletes only the obsolete `contextual_verdict_projections` row matching the current-key unique.
4. Deterministically replaces it with the next-most-recent valid Decision if one exists (by `(confirmed_at DESC, decision_id DESC)` ordering).

Direct `DELETE` on `contextual_verdict_projections` by API/worker roles remains denied by lack of grant. The function is the only path to remove an obsolete verdict row during rebuild/invalidation.

### Column-specific write-once triggers (PostgreSQL)

**`products.product_profile_entry_id`** — `trg_products_profile_set_once`: allow NULL→non-NULL once; reject non-NULL→different or non-NULL→NULL.

**`asset_versions` truth-dependency** — `trg_asset_versions_truth_write_once` (`BEFORE INSERT OR UPDATE`):
- On `INSERT`: rejects partial four-field group (some NULL, some non-NULL); rejects `truth_dependency_policy_version = 'v1-bootstrap'`. All-NULL is allowed (fail-closed pending determination).
- On `UPDATE`: if `OLD` has all four NULL, permits one atomic initialization where all four become non-NULL simultaneously, with `truth_dependency_policy_version != 'v1-bootstrap'`. Once any field is non-NULL, rejects any change to any of the four fields — no partial update, no clear, no value change.

This ensures `v1-bootstrap` can only come from the migration backfill (which runs before the trigger is installed). Post-cutover INSERTs and later UPDATEs cannot use `v1-bootstrap`.

**`experiments.record_schema_version`** — `trg_experiments_schema_version_set_once` (`BEFORE UPDATE`): rejects every UPDATE that changes `record_schema_version`, regardless of the old or new value. `NULL→2`, `NULL→NULL` (no change is no-op, not caught), `2→NULL`, `2→2` (same value), `2→3` — all mutations rejected. The server writes `2` on INSERT for schema-2 Experiments; legacy rows remain NULL permanently.

**`experiments.started_at`** — `trg_experiments_started_at_set_once` (`BEFORE UPDATE`): allow NULL→non-NULL once; reject subsequent change.

**`experiment_ad_bindings.record_schema_version`** — `trg_ad_bindings_schema_set_once` (`BEFORE UPDATE`): same semantics as `experiments.record_schema_version` — every UPDATE that changes the value is rejected. The server writes the value on INSERT; no row can switch between legacy and schema-2 CASE branches.

**`asset_qc_attempts` immutable fields** — `trg_qc_attempts_seq_write_once`: protects `attempt_sequence` and `remediates_attempt_id`. Lifecycle fields remain mutable.

### Controlled Workspace erasure

The approved §1 invariant is write-once/never-deleted **subject to the controlled Workspace erasure/retention procedure**. An unconditional reject-DELETE trigger with RESTRICT FKs would make workspace-level erasure impossible. The design provides a tightly scoped, privileged bypass:

**Shared trigger function with erasure bypass and role check**:

```sql
CREATE FUNCTION marketai_reject_update_or_delete() RETURNS TRIGGER AS $$
DECLARE
    erasure_ws TEXT;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'UPDATE denied on append-only table %', TG_TABLE_NAME;
    END IF;
    -- DELETE: require erasure principal role + matching workspace
    erasure_ws := current_setting('marketai.erasure_workspace_id', true);
    IF erasure_ws IS NOT NULL THEN
        IF NOT pg_has_role(session_user, 'marketai_erasure_principal', 'MEMBER') THEN
            RAISE EXCEPTION 'DELETE denied: caller is not marketai_erasure_principal on table %', TG_TABLE_NAME;
        END IF;
        IF OLD.workspace_id = erasure_ws THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'DELETE denied: workspace_id does not match erasure target on table %', TG_TABLE_NAME;
    END IF;
    RAISE EXCEPTION 'DELETE denied on append-only table %', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql SET search_path = '';
```

The eligibility projection no-delete trigger uses the same erasure-principal + workspace check.

Setting the `marketai.erasure_workspace_id` GUC alone is not authorization — any PostgreSQL user can set a custom GUC. The `pg_has_role` check ensures only callers who are members of `marketai_erasure_principal` can exercise the bypass.

**Erasure slice contract**: this migration defines a secure erasure manifest for its experiment-domain tables. No repository-wide workspace-erasure orchestrator or audit log table exists yet. A future orchestrator may invoke this slice; this migration does not claim to erase all repository data on its own.

**Erasure procedure**: `SECURITY DEFINER` function owned by a designated admin role that holds the `BYPASSRLS` attribute, with `SET search_path = ''`, `REVOKE ALL ON FUNCTION ... FROM PUBLIC`, and `GRANT EXECUTE ON FUNCTION ... TO marketai_erasure_principal`. The `BYPASSRLS` attribute on the owner role is required because `FORCE ROW LEVEL SECURITY` applies to table owners — only superusers and roles with `BYPASSRLS` bypass it. The migration verifies the owner role has `BYPASSRLS` during installation and raises if it does not. The procedure remains scoped to one workspace by the session variable and RESTRICT FK ordering.

The procedure:
1. Assert caller is `marketai_erasure_principal` via `pg_has_role`.
2. `SET LOCAL marketai.erasure_workspace_id = '<target_workspace_id>'`.
3. Delete in dependency-correct order:
   - `contextual_verdict_projections` (references: decisions, confirmations, candidates, versions, experiments)
   - `experiment_decisions` (references: confirmations, proposals, candidates, versions, judgements, experiments, entries)
   - `experiment_eligibility_projections` (references: versions, candidates, events)
   - `experiment_eligibility_events` (references: versions, candidates)
   - `decision_confirmations` (references: proposals)
   - `decision_proposal_state_events` (references: proposals)
   - `experiment_version_candidate_concepts` (references: candidates)
   - `experiment_ad_bindings` (references: entries, candidates)
   - `decision_proposals` (references: versions, judgements, experiments)
   - `experiment_version_concept_resolutions` (references: versions)
   - `experiment_version_candidates` (references: versions, entries)
   - `judgements` (references: versions, experiments)
   - `experiment_versions` (references: experiments)
   - `experiment_entries` (references: experiments)
   - `experiment_state_events` (references: experiments)
   - `asset_qc_check_results`, `asset_qc_attempts`, `quality_reviews` (workspace-scoped evidence included in this slice)
   - `experiments`
4. RESTRICT FKs enforce correct ordering — a misordered delete fails.
5. Audit receipt is emitted to an external/control-plane audit sink injected into the administrative workflow (not to a database table in this schema), ensuring the receipt survives the workspace data erasure independently of any repository-local storage.

**SQLite controlled erasure**: the service-level erasure path checks an `is_erasure_principal` flag on the session context, validates workspace scope, and deletes in the same dependency order. No trigger bypass is needed (SQLite has no triggers for these tables). The same dependency ordering and external audit logging apply.

### SQLite compatibility

- Partial indexes on both dialects via `postgresql_where`/`sqlite_where`.
- Nullable column adds: direct. NOT NULL changes: batch mode.
- No PostgreSQL triggers on SQLite — service guards and tests are defense-in-depth.
- All canonical hashes Python-side (§4.7).
- JSON columns use typed SQLAlchemy `SA_JSON` expressions for reads, ensuring structured dict return on both dialects (no raw string on SQLite).

---

## §4.10 Downgrade and recovery

### Guarded downgrade

Comprehensive preflight checking all non-re-derivable post-migration writes:

1. **New immutable/evidence tables**: any rows in `experiment_versions`, `experiment_version_candidates`, `experiment_version_candidate_concepts`, `experiment_version_concept_resolutions`, `decision_proposals`, `decision_proposal_state_events`, `decision_confirmations`, `experiment_eligibility_events`, `experiment_eligibility_projections`, `contextual_verdict_projections` → abort.
2. **Schema-2 experiments**: any `experiments.record_schema_version = 2` → abort.
3. **Non-NULL ProductProfile links**: any `products.product_profile_entry_id IS NOT NULL` → abort.
4. **Non-bootstrap truth determinations**: any `asset_versions.truth_dependency_policy_version IS NOT NULL AND != 'v1-bootstrap'` → abort.
5. **Non-NULL QC remediation links**: any `asset_qc_attempts.remediates_attempt_id IS NOT NULL` → abort.
6. **Duplicate preflight before restoring old UNIQUEs**: duplicate `frozen_snapshot_hash` → abort; duplicate `(ad_account_id, ad_id)` → abort.

**Data safe to drop** (deterministic, proven reproducible): backfilled `workspace_id` (from parent experiments), `v1-bootstrap` truth evidence (from `truth_package_id`), QC `evidence_hash` (from check-result columns), QC `attempt_sequence` (from `(created_at, id)` ordering), QC `terminal_at` (from `state`/`updated_at`).

`_do_downgrade()` reverses in reverse-dependency order: drop the shared `marketai_reject_update_or_delete()` function and its triggers; drop projection-maintenance function; restore #21's original per-table function `reject_update_delete_asset_qc_check_results()` and trigger `trg_asset_qc_check_results_immutable`; drop column-specific triggers on existing tables; drop new tables; remove added columns; restore original constraints.

### Once non-re-derivable writes exist

Downgrade explicitly impossible. Recovery is forward-fix or verified backup.

### Migration failure recovery

**PostgreSQL**: single DDL transaction; failure rolls back everything.
**SQLite**: batch DDL not atomic; restore from backup on partial failure.

---

## §4.11 Migration acceptance checks

### Upgrade/downgrade lifecycle
1. Clean upgrade on PostgreSQL succeeds.
2. Clean upgrade on SQLite succeeds.
3. Downgrade in empty-schema-2 case succeeds on both dialects.
4. Downgrade with schema-2 experiments, evidence rows, profile links, non-bootstrap truth, or remediation links aborts naming the blocker.
5. Downgrade preflights detect duplicate `frozen_snapshot_hash` and `(ad_account_id, ad_id)`.
6. Downgrade restores #21's original `asset_qc_check_results` per-table trigger.

### Tenant lineage and composite FKs
7. All child `workspace_id` matches parent experiment's `workspace_id`.
8. Cross-workspace FK insert fails.
9. All `workspace_id IS NULL` counts = 0 on backfilled tables.
10. Three-column lineage FK: eligibility event with mismatched experiment/version fails.

### Schema version discrimination
11. Ad bindings: legacy CHECK and schema-2 CHECK. Violation → failure.
12. Judgements: bidirectional CHECK. Violation → failure.
13. Decisions: full §1.8 bidirectional — legacy entry NOT NULL + ALL new NULL; schema-2 entry NULL + ALL new NOT NULL. Violation → failure.
14. `experiments.record_schema_version` CHECK: NULL or 2 only.

### Discriminator immutability
15. `experiments.record_schema_version`: UPDATE `NULL→2` rejected; UPDATE `2→NULL` rejected; UPDATE `2→2` rejected. Only INSERT with `2` succeeds.
16. `experiments.started_at` change after non-NULL set → rejected.
17. `experiment_ad_bindings.record_schema_version`: UPDATE `NULL→2` rejected; every UPDATE change rejected. Only INSERT sets the value.

### ProductProfile bridge
18. All existing rows NULL (no guessed backfill).
19. Partial unique prevents duplicate links within workspace.
20. Cross-workspace FK insert fails.
21. Set-once trigger: NULL→value succeeds; relink/clear rejected.

### Hash integrity
22. Hash equality across dialects on fixed test vector.
23. Truth-dependency hashes reproducible from `truth_package_id` + `v1-bootstrap` tuple.
24. QC evidence hashes reproducible from check-result columns.
25. Normalized-column equality to canonical snapshot.
26. `_canonical_json` rejects raw `float` input (via `_reject_floats`), not only via field validation; test `_canonical_json(1.5)` raises `TypeError`.
27. Canonical encoder rejects naive datetimes; preserves Decimal and microsecond precision.
28. Nested-JSON, offset-datetime, Decimal precision, legacy-float, and SQLite round-trip test vectors.
29a. `_canonical_decimal_str(Decimal("1000"))` emits `"1000"` (plain notation, not `"1E+3"`); `_canonical_decimal_str(Decimal("-0"))` emits `"0"` (negative zero collapsed).
29b. `_canonical_json({1: "value"})` raises `TypeError` for non-string dict key; `_canonical_json({"a": "value"})` succeeds.

### `eligibility_context_hash` sensitivity
30. Changing `frozen_snapshot_hash` (input 1) changes `eligibility_context_hash`.
31. Changing version-level projection head (input 2) changes hash.
32. Changing recheck evidence (input 4) changes hash.
33. Adding or removing a candidate from the set (input 3) changes hash.
34. Swapping two candidates' head hashes (keeping IDs) changes hash.
35. Changing the enumeration order of the same `(candidate_id, head_hash)` set does NOT change hash (canonical sorting).

### Eligibility event/projection determinism
36. Delayed-delivery test: events with server-assigned monotonic `observed_at` produce identical `head_hash` whether folded incrementally or rebuilt.
37. Equal-wall-clock test: two events assigned same wall clock → service advances by minimum tick; rebuild order matches.
38. Rebuild verifies `head_hash`, `head_sequence`, `current_eligibility`, and `irreversible_active` match pre-rebuild state.
39. Baseline `head_hash` is non-NULL and deterministic.
40. Projection head CAS rejects stale `head_sequence`.

### Proposal/confirmation/decision pipeline
41. Proposal event sequence unique.
42. Confirmation per-proposal unique.
43. Confirmation idempotency-key unique.
44. Concurrent confirmation race: one succeeds, one fails.

### Lineage mismatch tests
45. Proposal with Judgement from different experiment → rejected (database FK: 4-column FK `(workspace_id, experiment_id, experiment_version_id, judgement_id)→judgements`).
46. Proposal with Judgement from different version → rejected (database FK: same 4-column FK).
47. Proposal with Version from different experiment → rejected (database FK: 3-column FK `(workspace_id, experiment_id, experiment_version_id)→experiment_versions`).
48. Decision with candidate from different version → rejected (database FK: 3-column FK `(workspace_id, experiment_version_id, experiment_version_candidate_id)→experiment_version_candidates`).
49. Decision with confirmation from different proposal → rejected (database FK: 3-column FK `(workspace_id, confirmation_id, proposal_id)→decision_confirmations`).
50. Decision with `experiment_id` differing from Proposal's → rejected (database FK: 5-column FK `(workspace_id, proposal_id, experiment_id, experiment_version_id, judgement_id)→decision_proposals`).
51. Decision with `experiment_version_id` differing from Proposal's → rejected (same 5-column FK).
52. Decision with `judgement_id` differing from Proposal's → rejected (same 5-column FK).
53. Verdict projection combining IDs from different versions → rejected (service validation).
54. Verdict projection with `asset_version_id` differing from Candidate's → rejected (service validation).
55. Verdict projection with `product_version_id` differing from Version's → rejected (service validation).
56. Verdict projection with context fields (market, placement, offer, landing page hash) not matching Version → rejected (service validation).

### Immutability enforcement
57. UPDATE on each `APPEND_ONLY_TABLES` entry → rejected.
58. DELETE on each `APPEND_ONLY_TABLES` entry → rejected (non-erasure context).
59. Eligibility projections: UPDATE allowed; DELETE rejected.
60. Verdict projections: general API/worker DELETE → denied (no grant); `marketai_projection_writer` DELETE via projection-maintenance function → allowed.
61. Column write-once triggers: profile relink rejected; truth mutation rejected; `v1-bootstrap` on post-cutover INSERT rejected; QC sequence/remediation mutation rejected.

### Projection-maintenance function
62. Projection-maintenance function correctly deletes obsolete current-key row and replaces with next-most-recent valid Decision (replacement INSERT succeeds via `marketai_projection_writer` grants).
63. Direct `DELETE` by API/worker role on `contextual_verdict_projections` denied (no grant). API/worker roles are not members of `marketai_projection_writer` and cannot `SET ROLE` to it.
64. Projection-maintenance function validates caller's tenant context.
65. API/worker role invokes projection-maintenance function on the replacement path: obsolete row deleted, replacement row inserted atomically through the SECURITY DEFINER function, function returns success. Same role's direct DELETE on verdict projections is denied (no grant).

### Real PostgreSQL trigger installation
66. Shared function `marketai_reject_update_or_delete` exists after migration.
67. Every trigger in `APPEND_ONLY_TABLES` exists in `pg_trigger` (qualified by `tgrelid`) after migration.
68. Every column write-once trigger exists in `pg_trigger`.
69. No `CREATE TRIGGER IF NOT EXISTS` in migration DDL (doesn't exist in PostgreSQL).
70. #21's original `asset_qc_check_results` per-table function/trigger are replaced, not duplicated.
71. Projection-maintenance function exists with correct ownership (`marketai_projection_writer`), SECURITY DEFINER, locked `search_path`, PUBLIC execute revoked, EXECUTE granted to API/worker roles, and `marketai_projection_writer` has the minimum grants listed in the role/grant matrix (SELECT on immutable/existing tables, SELECT+UPDATE on eligibility projections, full DML on verdict projections).

### SQLite JSON round-trip
72. Insert structured JSON object via typed `SA_JSON` binding on SQLite, read back, assert structural equality (not integer 0).
73. QC evidence hash: read `evidence_json` via typed `SA_JSON` column on SQLite, normalize, hash — result matches PostgreSQL hash for same data.

### Controlled erasure
74. Erasure for one workspace removes all its experiment-domain data in the dependency-correct order listed in §4.9.
75. Erasure does not affect rows in other workspaces (cross-workspace-negative test).
76. Non-erasure DELETE on append-only tables remains rejected.
77. RESTRICT FKs enforce correct deletion order — misordered delete fails.
78. Ordinary role (API/worker) setting `marketai.erasure_workspace_id` GUC and attempting DELETE → rejected by `pg_has_role` check in trigger.
79. Audit receipt is emitted to external sink and survives the workspace erasure.
80. Erasure procedure owner role has `BYPASSRLS` attribute (verified during migration installation).

### Truth and QC invariants
81. Bootstrap classification: NULL `truth_package_id` → `expected=false`; non-NULL → `expected=true`.
82. Gate 13 fail-closed for NULL `truth_dependency_expected`.
83. Truth CHECKs: `expected=false` + non-NULL truth_package → violation; non-NULL truth_package + `expected!=true` → violation.
84. Truth write-once: mutation after initial set → rejected.
85. Bootstrap rejection: post-cutover INSERT with `v1-bootstrap` policy → rejected by trigger. Post-cutover UPDATE initialization with `v1-bootstrap` → rejected by trigger.
86. QC sequence uniqueness.
87. QC remediation invariants (service tests).
88. Active-attempt partial unique.

---

## §4.12 Traceability matrix

Every accepted Section-1 field and Section-2/3 schema delta appears exactly once.

| Approved record / field group | Physical table(s) | Key constraints |
|---|---|---|
| ExperimentVersionRecord (§1) | `experiment_versions` | UNIQUE(experiment_id); UNIQUE(workspace_id, id); UNIQUE(workspace_id, experiment_id, id); integrity projections verified at insert |
| — canonicalization_version, snapshot | `.canonicalization_version`, `.frozen_snapshot_hash` | INDEX not UNIQUE |
| — metric_contract_hash | `.metric_contract_hash` | integrity projection |
| — market, placement, landing page | `.market`, `.placement`, `.landing_page_evidence_hash` | |
| — audience | `.audience_snapshot_json`, `.audience_snapshot_hash` | |
| — budget | `.daily_budget_amount`, `.total_budget_cap`, `.budget_currency` | Numeric(18,6) |
| — schedule | `.schedule_start_at`, `.schedule_end_at`, `.schedule_timezone` | String(100) |
| — attribution windows | `.attribution_click_window_days`, `.attribution_view_window_days` | 1–90 CHECK |
| — statistical plan | `.statistical_plan_json`, `.statistical_plan_hash` | |
| — product/offer version | `.product_version_id`, `.offer_version_id` | composite FKs RESTRICT |
| — hash chain | `.candidates_hash`, `.concept_resolutions_hash` | |
| ExperimentVersionCandidateRecord | `experiment_version_candidates` | UNIQUE(w,id); UNIQUE(w,ev,id) |
| — source provenance | `.source_entry_id`(nullable), `.source_kind` CHECK(generation\|upload\|reuse\|manual), `.source_run_item_id`(nullable), `.source_evidence_*` | composite FKs |
| — asset identity | `.asset_version_id`, `.asset_version_content_sha256` | composite FK |
| — hash chain | `.marketing_vars_hash`, `.concepts_hash`, `.candidate_hash` | |
| CandidateConceptRecord | `experiment_version_candidate_concepts` | UNIQUE(experiment_version_candidate_id, dimension_key); `duplicate_group_hash` NOT NULL |
| ConceptResolutionRecord | `experiment_version_concept_resolutions` | UNIQUE(version_id, duplicate_group_hash); source fingerprints |
| DecisionProposalRecord | `decision_proposals` | UNIQUE(w, judgement_id, eligibility_context_hash); UNIQUE(w, id, experiment_id, experiment_version_id, judgement_id) full lineage anchor; no created_by; 4-col FK to judgements; 3-col FK to versions |
| — eligibility_context_hash | §4.2.1 formula | 4-input canonical tuple; sorting-order-invariant |
| — proposal_hash | commits context+judgement+verdicts | |
| ProposalStateEventRecord | `decision_proposal_state_events` | UNIQUE(w, proposal_id, sequence); CAS |
| DecisionConfirmationRecord | `decision_confirmations` | UNIQUE(w, proposal_id); UNIQUE(w, idempotency_key_hash); UNIQUE(w, id, proposal_id) lineage anchor; no experiment_id (derives through proposal) |
| EligibilityEventRecord (§2.5) | `experiment_eligibility_events` | 3-col lineage FK; two dedup partials; monotonic observed_at |
| Eligibility projections (§2.5, §3.11) | `experiment_eligibility_projections` | partial uniques; CAS; non-NULL baseline head_hash; in-place rebuild; no DELETE |
| ContextualVerdictProjection (§1.10) | `contextual_verdict_projections` | current-key UNIQUE(w, exp, av, offer, market, placement, lpe_hash); projection-maintenance function for DELETE; full lineage validation including asset/context field equality |
| ProductProfile bridge | `products.product_profile_entry_id` | composite FK; partial unique; set-once trigger |
| record_schema_version | `experiments.record_schema_version` | CHECK(NULL or 2); INSERT-only; UPDATE always rejected |
| started_at | `experiments.started_at` | set-once trigger |
| Bidirectional binding CHECK | `experiment_ad_bindings` CASE CHECK | INSERT-only discriminator; UPDATE always rejected |
| Bidirectional judgement CHECK | `judgements` CASE CHECK | 3-col lineage FK; UNIQUE(w, exp, ev, id) 4-col anchor |
| Bidirectional decision CHECK (§1.8) | `experiment_decisions` full CASE CHECK | 5-col FK `(w, proposal_id, experiment_id, experiment_version_id, judgement_id)→proposals` enforces experiment+version+judgement match; 3-col FK to candidates enforces version match; 3-col FK to confirmations enforces proposal match; context_hash = SHA-256(exp, offer, market, placement, lpe_hash); actor/confirmed_at from Confirmation; 3 service-enforced residuals |
| Truth-dependency (§2.4) | `asset_versions` +4 cols, 3 CHECKs | BEFORE INSERT OR UPDATE trigger; atomic group; v1-bootstrap rejection |
| QC evolution (§2.2) | `asset_qc_attempts` +3 cols | write-once on seq/remediation; remediation invariants |
| QC evidence hash | `asset_qc_check_results.evidence_hash` | NOT NULL; typed SA_JSON read; no double-encoding |
| Lock order (§2.1) | service enforcement | projection sentinels for class-5 |
| Append-only existing evidence | quality_reviews, state_events, judgements, decisions, asset_qc_check_results | shared erasure-aware function; #21 trigger replaced |
| Controlled erasure (§1) | `pg_has_role` + session-variable trigger bypass; SECURITY DEFINER procedure with BYPASSRLS owner | workspace-scoped erasure slice contract; dependency-correct order; audit receipt to external sink |
| Projection maintenance | `marketai_maintain_verdict_projection()` SECURITY DEFINER | owned by projection_writer with minimum grants (SELECT on source tables, SELECT+UPDATE on eligibility projections for class-5 lock, full DML on verdict projections); EXECUTE to api/worker; no membership grant; validates tenant + class-5 lock; replacement INSERT tested |
| Canonical encoding | `_canonical_decimal_str`, `_reject_floats` | plain notation (no exponent); negative zero → "0"; non-string key rejection |

---

## §4.13 Deferred (not defined in Section 4)

- ORM model class updates — implementation detail.
- Performance benchmarks for backfill — deployment planning.
- Automated evidence and test plan — Section 5.


<!-- source-boundary: approved-section-delta -->
# Issue #22 — Approved-Section Delta

> **Status**: Normative within the complete design candidate.
> Takes precedence only where it explicitly names an approved clause.
> Does NOT edit approved Section 2/3/4 files or their SHA-256 hashes.

---

## Section 2 deltas

### D-S2-01: Global lock order gains class 7

**Modifies**: approved §2.4 (concurrency linearization contract, 6-class lock order).

The complete design adds class 7 after the existing 6 classes:

- **Class 7**: `ExperimentTimelineHead` — acquired after class 6 (`Proposal`).
- All flows that allocate timeline sequences lock class 7 last.
- Updated global lock order: 1 → 2 → 3 → 4 → 5 → 6 → 7.

No existing class changes identity or relative order.

### D-S2-02: Timeline, notification, next-round, and pattern evidence contracts

**Extends**: approved §2 domain model.

Approved §2 gains domain model contracts for:

- `ExperimentTimelineHeadRecord` and `ExperimentTimelineEventRecord` (§A2) — per-experiment monotonic sequence with vocabulary-controlled event types.
- `ExperimentNotificationOutboxRecord` (§A5) — fully append-only, no delivery columns; durable notification-intent creation with source-dedup UNIQUE.
- `ExperimentNextRoundRecord` (§A6) — immutable record linking a material Decision to a new `GenerationDraft` via composite lineage FKs.
- `ExperimentPatternCandidateRecord` (§A7) — immutable record from confirmed material Decision.

All are append-only evidence (timeline heads are the sole exception: UPDATE on `head_sequence` is allowed for sequence allocation).

---

## Section 3 deltas

### D-S3-01: List endpoint

**Extends**: approved §3.12 (read endpoints).

`GET /api/experiments` (§A1) with truthful keyset pagination:

- Signed keyset cursor binding `(last_created_at, last_id, workspace_id, schema_version_filter, page_size, direction)`.
- No cross-request database snapshot guarantee. Deterministic ordering and no duplicate delivery of an unchanged row in one forward traversal.
- Page-specific ETag: hash of canonical query identity plus ordered `(experiment_id, revision)` for returned items and `next_cursor`. `If-None-Match` valid only for the identical page request.

### D-S3-02: Timeline endpoint

**Extends**: approved §3.12 (read endpoints).

`GET /api/experiments/{id}/timeline` (§A2) with cursor pagination ordered by `(timeline_sequence ASC, id ASC)`. Snapshot boundary via `head_sequence` at first-page query time.

### D-S3-03: Next-round command

**Extends**: approved §3 (commands).

`POST /api/experiments/{experiment_id}/decisions/{decision_id}/next-round` (§A6).

| Aspect | Value |
|---|---|
| Permission | `experiment.next_round`; workspace membership required |
| `If-Match` | Required (`428 precondition_required` if missing); stale revision → `409 revision_conflict` |
| `Idempotency-Key` | Required (`400 idempotency_key_required` if missing, per approved §3.2) |
| State precondition | Experiment `closed` with `closure_kind = completed` (`409 experiment_not_closed` / `409 wrong_closure_kind`) |
| Materiality precondition | Decision must be material; non-material → `422 decision_not_material` |
| Composite lineage FKs | Decision 5-col (`D-S4-01`), Candidate 4-col (`D-S4-01`), QC-attempt 3-col nullable (`D-S4-01`) |
| `source_candidate_id` | Required non-null field |
| Replay | Same Idempotency-Key → return existing record (`200`) |
| Success | `201 Created` with `ExperimentNextRoundRecord` + `GenerationDraft` + timeline event |
| Other errors | `409 schema_version_mismatch` (wrong schema version); `404` (foreign workspace or resource lineage not found); `422` (request field validation failure) |
| Error mapping | Per §A6 contract table; normative reference to §A6 for body schema and algorithm detail |

### D-S3-04: Pattern-candidate command

**Extends**: approved §3 (commands).

`POST /api/experiments/{experiment_id}/decisions/{decision_id}/pattern-candidates` (§A7).

| Aspect | Value |
|---|---|
| Permission | `experiment.create_pattern`; workspace membership required |
| `If-Match` | Required (`428 precondition_required` if missing); stale revision → `409 revision_conflict` |
| `Idempotency-Key` | Required (`400 idempotency_key_required` if missing, per approved §3.2) |
| State precondition | Experiment `closed` with `closure_kind = completed` (`409 experiment_not_closed` / `409 wrong_closure_kind`) |
| Materiality precondition | Decision must be material; non-material → `422 decision_not_material` |
| Composite lineage FKs | Decision 5-col (`D-S4-01`), Candidate 3-col (existing UNIQUE) |
| Replay | INSERT-or-load on `UNIQUE(workspace_id, decision_id)` → return existing (`200`) |
| Success | `201 Created` with `ExperimentPatternCandidateRecord` + timeline event |
| Other errors | `409 schema_version_mismatch` (wrong schema version); `404` (foreign workspace or resource lineage not found); `422` (request field validation failure) |
| Error mapping | Per §A7 contract table; normative reference to §A7 for body schema and algorithm detail |

### D-S3-05: Below-minimum candidate HTTP status

**Modifies**: approved §3.2 (precondition/error mapping).

`insufficient_candidates_after_resolution` → HTTP 409, aligning with `candidate_limit_exceeded`.

### D-S3-06: Replay taxonomy completeness

**Extends**: approved §3.2 (replay classes).

Replay classes gain notification deduplication proof: append-only outbox + timeline source-dedup UNIQUE ensures no replay class produces duplicate notification intent (§A4.5, §A5).

---

## Section 4 deltas

### D-S4-01: Composite lineage anchors on existing tables

**Modifies**: approved §4.3/§4.6 existing table constraints.

Additional UNIQUE constraints as composite FK parent anchors for addendum tables:

| Table | New UNIQUE | Purpose |
|---|---|---|
| `experiment_version_candidates` | `UNIQUE(workspace_id, experiment_version_id, id, asset_version_id)` | Parent anchor for NextRound candidate FK (4-column, includes `asset_version_id`) |
| `experiment_decisions` | `UNIQUE(workspace_id, id, experiment_id, experiment_version_id, experiment_version_candidate_id)` | Parent anchor for NextRound and PatternCandidate Decision-lineage FK (5-column) |
| `asset_qc_attempts` | `UNIQUE(workspace_id, id, asset_version_id)` | Parent anchor for NextRound QC-attempt FK |

**Note**: `experiment_version_candidates` already has `UNIQUE(workspace_id, experiment_version_id, id)` (approved §4, experiment_version_candidates definition) which serves as the parent anchor for PatternCandidate's 3-column candidate FK. No additional constraint needed for that FK.

These UNIQUE constraints are redundant with existing uniqueness (every `(workspace_id, id)` is already unique) but are required by PostgreSQL to serve as FK parent references — a FK must reference the exact column set of a declared UNIQUE or PK constraint.

**Child composite lineage FKs referencing these anchors:**

| Child table | FK columns | Parent anchor |
|---|---|---|
| `experiment_next_round_records` | `(workspace_id, decision_id, experiment_id, experiment_version_id, source_candidate_id)` | `experiment_decisions` 5-col UNIQUE |
| `experiment_next_round_records` | `(workspace_id, experiment_version_id, source_candidate_id, source_asset_version_id)` | `experiment_version_candidates` 4-col UNIQUE |
| `experiment_next_round_records` | `(workspace_id, qc_attempt_id, source_asset_version_id)` NULLABLE | `asset_qc_attempts` 3-col UNIQUE |
| `experiment_pattern_candidates` | `(workspace_id, decision_id, experiment_id, experiment_version_id, candidate_id)` | `experiment_decisions` 5-col UNIQUE |
| `experiment_pattern_candidates` | `(workspace_id, experiment_version_id, candidate_id)` | `experiment_version_candidates` existing 3-col UNIQUE |

### D-S4-02: Addendum tables

**Extends**: approved §4.8 step 5 (new table creation).

Five new tables appended to migration step 5 (after item 10, `contextual_verdict_projections`):

| Order | Table | Trigger model |
|---|---|---|
| 11 | `experiment_timeline_heads` | Mutable (UPDATE on `head_sequence` allowed; identity columns immutable) |
| 12 | `experiment_timeline_events` | Append-only |
| 13 | `experiment_notification_outbox` | Append-only (no delivery columns) |
| 14 | `experiment_next_round_records` | Append-only |
| 15 | `experiment_pattern_candidates` | Append-only |

All tables:
- `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`
- Policy: `USING (marketai_context_is_current(workspace_id)) WITH CHECK (marketai_context_is_current(workspace_id))`
- Composite workspace FKs following approved `(workspace_id, entity_id) → parent` pattern
- Triggers: items 12–15 (`experiment_timeline_events`, `experiment_notification_outbox`, `experiment_next_round_records`, `experiment_pattern_candidates`) use the shared `marketai_reject_update_or_delete()` append-only trigger. Item 11 (`experiment_timeline_heads`) uses an identity-immutable trigger that rejects UPDATE on identity columns (`id`, `workspace_id`, `experiment_id`) while allowing UPDATE on `head_sequence` (API only, per D-S4-03).

### D-S4-03: Role grants for addendum tables

**Extends**: approved §4.9 role/grant matrix.

Exact least-privilege grants per addendum table:

| Table | `marketai_domain_api` | `marketai_domain_worker` | `marketai_projection_writer` | `marketai_domain_readonly` |
|---|---|---|---|---|
| `experiment_timeline_heads` | SELECT, INSERT, UPDATE | SELECT, INSERT | SELECT | SELECT |
| `experiment_timeline_events` | SELECT, INSERT | SELECT, INSERT | SELECT | SELECT |
| `experiment_notification_outbox` | SELECT, INSERT | SELECT, INSERT | SELECT | SELECT |
| `experiment_next_round_records` | SELECT, INSERT | SELECT, INSERT | SELECT | SELECT |
| `experiment_pattern_candidates` | SELECT, INSERT | SELECT, INSERT | SELECT | SELECT |

- UPDATE on `experiment_timeline_heads` (`head_sequence`) allowed only for `marketai_domain_api`. No other runtime role has UPDATE on any addendum table.
- No DELETE on any addendum table for any runtime role.
- `marketai_domain_worker` has SELECT + INSERT only on all addendum tables; no UPDATE or DELETE.
- `marketai_projection_writer` gets SELECT only on addendum tables — no INSERT/UPDATE/DELETE.
- Erasure is handled exclusively through the erasure procedure (see D-S4-04).

### D-S4-04: Erasure role matrix correction

**Replaces**: approved §4.9 role/grant matrix row:

```
| marketai_erasure_principal | ALL (workspace-scoped) | ALL (workspace-scoped) | ALL (workspace-scoped) | ALL (workspace-scoped) |
```

With the complete design's procedure-only caller/owner split:

**`marketai_erasure_principal`** (caller role):
- EXECUTE on the erasure SECURITY DEFINER procedure only.
- No direct table DML grants (no SELECT, INSERT, UPDATE, DELETE on any table).
- No BYPASSRLS.
- Is not a member of `marketai_domain_api`, `marketai_domain_worker`, `marketai_projection_writer`, or `marketai_domain_readonly`.
- Cannot invoke any other SECURITY DEFINER function.

**Designated admin OWNER** of the erasure procedure:
- BYPASSRLS — required because the procedure deletes across all workspaces' rows in the correct dependency order.
- Owns the SECURITY DEFINER erasure procedure.
- Has the internal DML privileges required by the procedure body.
- Is not a runtime membership target — no application connection uses this role.
- `REVOKE EXECUTE ON FUNCTION marketai_erase_workspace(UUID) FROM PUBLIC` — only `marketai_erasure_principal` has EXECUTE.

The erasure procedure itself performs workspace-scoped DML internally using the owner's BYPASSRLS privilege. The caller (`marketai_erasure_principal`) triggers it but cannot bypass RLS or perform direct table operations.

This applies to both approved §4 tables and addendum tables uniformly.

### D-S4-05: Erasure dependency order extension

**Extends**: approved §4.9 erasure dependency order.

Addendum tables are inserted as leaves before existing experiment evidence tables. Extended order (leaf → root):

1. `experiment_pattern_candidates` (leaf — depends on experiments, versions, decisions, candidates)
2. `experiment_next_round_records` (depends on experiments, versions, decisions, asset_versions, generation_drafts, optionally qc_attempts)
3. `experiment_notification_outbox` (depends on timeline_events)
4. `experiment_timeline_events` (depends on experiments)
5. `experiment_timeline_heads` (depends on experiments)
6. ... (existing experiment evidence tables per approved §4.9)
7. ... (existing experiments, workspaces)

RESTRICT FKs block misordered deletion. The erasure procedure enforces this exact order.

### D-S4-06: Migration step 5 and step 7 extensions

**Extends**: approved §4.8 steps 5 and 7.

**Step 5 extension** — after item 10 (`contextual_verdict_projections`), add items 11–15 (see D-S4-02).

**Step 7 extension** — after triggers/RLS/grants for approved tables, add:
- Append-only triggers for items 12–15 (timeline_events, outbox, next_round, pattern_candidates).
- Mutable-identity trigger for item 11 (timeline_heads: identity columns immutable, `head_sequence` mutable).
- RLS ENABLE + FORCE + policy for all five tables.
- Grants per D-S4-03.

### D-S4-07: Downgrade preflight for addendum tables

**Extends**: approved §4.8 step 8 (downgrade preconditions).

Before dropping addendum tables, the downgrade checks:

| Table | Preflight | Blocking? |
|---|---|---|
| `experiment_next_round_records` | Abort if any rows exist | Yes — non-re-derivable |
| `experiment_pattern_candidates` | Abort if any rows exist | Yes — non-re-derivable |
| `experiment_timeline_events` | May drop (re-derivable from source events) | No |
| `experiment_notification_outbox` | May drop (re-derivable from timeline events) | No |
| `experiment_timeline_heads` | May drop (re-derivable) | No |

Drop order follows leaf→root (reverse of creation per D-S4-05).

### D-S4-08: ATI acceptance tests

**Extends**: approved §4.11 acceptance checks.

The 88 existing checks gain ATI-01..ATI-41 tests (§A10.8, 07/12/13 retired = 38 active) for addendum table integration. These test RLS, grants (including Worker no-UPDATE/no-DELETE, API UPDATE on timeline_heads only, ProjectionWriter SELECT success, erasure caller/owner split with zero DML and no EXECUTE on other SECDEF, PUBLIC EXECUTE revoked), triggers, composite lineage FKs, erasure dependency order, and SQLite guards for each addendum table.

---

## Precedence

This file is **normative within the complete design candidate**. Where a delta explicitly names an approved clause (by section, subsection, or table), the delta takes precedence over the corresponding text in approved Sections 2, 3, or 4 — but only for the purpose of this design candidate. Approved Section 2/3/4 source files remain unmodified and their SHA-256 hashes are unchanged.

Deltas that extend (add new contracts, tables, tests) do not conflict with approved content. Deltas that modify (D-S2-01 lock order, D-S4-04 erasure role) explicitly replace the named approved clause within this design candidate's scope.


<!-- source-boundary: cross-section-addendum -->
# Issue #22 — Cross-section addendum

This addendum defines normative additions to the complete design candidate for Issue #22 scope items not fully covered by approved Sections 2–4. Each item is classified as:

- **Approved mapping**: the behavior is defined in approved sections; no new contract needed.
- **Normative addition in complete design candidate — Section N extension**: new contract text included in the design candidate. The corresponding Section 5 tests are part of the design candidate.

All additions are part of a single holistic design candidate. There are zero unresolved product-semantic decisions and zero known design gaps. `design_complete = true`. `implementation_ready = true` — holistic final-design approval granted 2026-09-03. Complete design SHA-256: `1210427eb9f6b479b84f167d435c7c354785c1f4c9b1d6a3b60f49245f902f8e`.

All identifier columns use the project's canonical `String(64)` type. Composite workspace foreign keys follow the approved `(workspace_id, entity_id) → parent` pattern. `concept_key_id` is `String(64)` because platform keys have `workspace_id IS NULL`.

---

## A1. Experiment list endpoint

**Classification: Normative addition in complete design candidate — Section 3 extension.**

`GET /api/experiments` is not defined in approved §3.12. The Issue #22 scope requires "list/detail/timeline APIs."

### §3.12 extension: `GET /api/experiments`

| Field | Value |
|---|---|
| Method / Path | `GET /api/experiments` |
| Authorization | Workspace membership required; tenant-scoped via signed context |
| Workspace scope | Returns only experiments where `workspace_id` matches the caller's signed `marketai.workspace_id` |
| Schema-version filter | Optional query parameter `?schema_version=2` filters by `record_schema_version`; omitted returns all |
| Stable sort key | `(created_at DESC, id DESC)` — deterministic total order; `id DESC` breaks timestamp ties |
| Cursor | Signed, opaque; encodes `(last_created_at, last_id, workspace_id, schema_version_filter, page_size, direction)`. No snapshot-high-water fields. Cursor signature is bound to workspace, schema-version filter, page-size, and direction; a cursor signed for different query parameters is rejected |
| Keyset predicate | Descending: `WHERE (created_at, id) < (last_created_at, last_id)`. Uses row-value comparison for correct composite ordering |
| Page-size bounds | Default 25, max 50; `?page_size=N` outside `[1, 50]` → `422 invalid_page_size` |
| Response projection | `{ id, name, state, record_schema_version, created_at, updated_at, closure_kind? }` per experiment |
| ETag | Page-specific: hash of canonical query identity plus ordered `(experiment_id, revision)` for returned items plus `next_cursor`. `If-None-Match` is valid only for the identical page request. Do not echo a first-page ETag as a snapshot token on later pages |
| Success | 200 with `{ items: [...], next_cursor?: "...", etag: "..." }` |
| Empty | 200 with `{ items: [] }` |
| Consistency guarantee | Deterministic ordering and no duplicate delivery of an unchanged row in one forward traversal. Does **not** promise a cross-request database snapshot or no gaps under concurrent commits. Concurrent inserts/updates may appear or be absent according to their committed sort key. Clients restart from page 1 to refresh |
| Cursor reuse | A cursor signed for `?schema_version=2` is rejected for `?schema_version=` (all) or vice versa; HMAC signature binds filter parameters |

### Required Section 4 delta

- RLS policy on `experiments` already covers workspace scoping via `marketai_context_is_current(workspace_id)`.
- No new tables or columns required.
- API role already has `SELECT` on `experiments`.

### Section 5 tests

| Test ID | Scenario | Expected |
|---|---|---|
| AL-01 | List experiments in workspace — static set | 200; all workspace-scoped rows returned; no gaps, no duplicates in one forward traversal |
| AL-02 | Empty workspace | 200 with empty items |
| AL-03 | Schema-version filter | Only matching `record_schema_version` rows |
| AL-04 | Cursor keyset pagination | Stable ordering; no duplicates; no gaps of unchanged rows |
| AL-05 | Page-size out of bounds | `422 invalid_page_size` |
| AL-06 | Cross-workspace isolation | Other workspace's experiments not visible |
| AL-07 | Authorization: non-member | 403 |
| AL-08 | Same-timestamp tie-breaking | Two experiments with identical `created_at`; `id DESC` breaks tie deterministically |
| AL-09 | Signed cursor tampered | 400 invalid cursor |
| AL-10 | Cursor reuse with changed filter | Cursor signed for `?schema_version=2` rejected for unfiltered query |
| AL-11 | Page-specific ETag present | ETag in response; matches canonical query identity + returned items |
| AL-12 | If-None-Match for identical page | 304 when page content unchanged |
| AL-13 | Concurrent insert during traversal | Weak consistency: inserted row may or may not appear in subsequent pages; no crash, no duplicate of existing rows |
| AL-14 | Restart from page 1 picks up new rows | Fresh first-page query includes previously absent concurrent inserts |

---

## A2. Timeline endpoint

**Classification: Normative addition in complete design candidate — Section 2 + Section 3 + Section 4 extension.**

Approved §3.12 defines per-entity paginated reads but no unified timeline. Independent cursor-paginated streams cannot be merged into a coherent complete timeline client-side without cross-stream snapshot/order semantics.

### §2 extension: Timeline domain model

**`ExperimentTimelineHeadRecord`** — one row per experiment; locked as lock class 7 (after Proposal class 6) when allocating sequence numbers. Both command flows (freeze, judge, confirm, close) and upstream invalidation flows lock class 7 last, preserving the existing 6-class global lock order.

| Field | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | NOT NULL |
| `experiment_id` | String(64) | NOT NULL |
| `head_sequence` | BIGINT | NOT NULL; default 0 |
| | | `UNIQUE(workspace_id, experiment_id)` |
| | | Composite FK: `(workspace_id, experiment_id) → experiments` |

**`ExperimentTimelineEventRecord`** — append-only event log with a per-experiment monotonic sequence allocated under the timeline-head lock.

| Field | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | NOT NULL |
| `experiment_id` | String(64) | NOT NULL |
| `timeline_sequence` | BIGINT | NOT NULL |
| `event_type` | String(64) | NOT NULL; CHECK vocabulary below |
| `source_resource_type` | String(64) | NOT NULL |
| `source_resource_id` | String(64) | NOT NULL |
| `actor_id` | String(100) | Nullable |
| `summary_json` | JSON | NOT NULL; bounded ≤ 2 KB; identifiers/hashes/scalars only |
| `observed_at` | DateTime(tz) | NOT NULL; display timestamp from source event |
| `created_at` | DateTime(tz) | NOT NULL; server-assigned insertion time |
| | | `UNIQUE(workspace_id, experiment_id, timeline_sequence)` |
| | | `UNIQUE(workspace_id, source_resource_type, source_resource_id, event_type)` — deduplication |
| | | Composite FK: `(workspace_id, experiment_id) → experiments` |

`source_resource_type` + `source_resource_id` are non-FK references validated by same-transaction service logic. A polymorphic FK to multiple source tables is not viable; service validation plus the source-dedup unique constraint ensures referential integrity within the transaction.

**Sequence allocation protocol**: Lock `experiment_timeline_heads` row (class 7), read `head_sequence`, increment, write back, INSERT event with new sequence, commit. The head row is created when the experiment is created (sequence starts at 0; first event gets 1).

**Lock-order delta**: Append class 7 (`ExperimentTimelineHead`) after class 6 (`Proposal`). All flows that allocate timeline sequences lock class 7 last. Updated order: 1→2→3→4→5→6→7.

**Event-type vocabulary:**

| Event type | Source resource type | Emitted when |
|---|---|---|
| `experiment_created` | `experiment` | Experiment created |
| `experiment_frozen` | `experiment_version` | Version frozen |
| `experiment_started` | `experiment` | Experiment started |
| `experiment_closed` | `experiment` | Experiment closed (any closure_kind) |
| `eligibility_irreversible` | `eligibility_event` | Irreversible eligibility event appended |
| `judgement_created` | `judgement` | Judgement created |
| `proposal_created` | `proposal` | Proposal created |
| `proposal_invalidated` | `proposal_state_event` | Proposal terminally invalidated |
| `confirmation_created` | `confirmation` | Confirmation created |
| `decisions_materialized` | `confirmation` | Material Decisions created (same source as confirmation) |
| `next_round_created` | `next_round_record` | Next-round action completed |
| `pattern_candidate_created` | `pattern_candidate` | Pattern candidate created |

**Events that NEVER produce timeline entries**: reversible eligibility events (may self-resolve), projection rebuilds, same-Idempotency-Key replays, INSERT-or-load deduplication, eligibility-event replay dedup.

**Legacy backfill**: Deterministic backfill from existing lifecycle/judgement/decision evidence. For each experiment: scan existing experiment rows (created_at), experiment_versions (frozen), judgements, confirmations, decisions, and close events. Assign timeline_sequence by `(observed_at ASC, id ASC)` order. Head initialized to max assigned sequence. Legacy experiments without schema-2 evidence get `experiment_created` only. Backfill is idempotent via the source-dedup unique constraint.

### §3.12 extension: `GET /api/experiments/{id}/timeline`

| Field | Value |
|---|---|
| Method / Path | `GET /api/experiments/{id}/timeline` |
| Authorization | Workspace membership; tenant-scoped |
| Schema-version dispatch | Shared URL; dispatches by `record_schema_version` |
| Stable total order | `(timeline_sequence ASC, id ASC)` — `observed_at` is display data, not sort key |
| Cursor | Signed, opaque; encodes `(last_timeline_sequence, last_id, snapshot_high_water_sequence)`. `snapshot_high_water_sequence` is `head_sequence` at first-page query time. Subsequent pages return only events with `timeline_sequence <= snapshot_high_water_sequence` |
| Page-size bounds | Default 50, max 100; out of range → `422 invalid_page_size` |
| Event summary | `{ timeline_sequence, event_type, source_resource_type, source_resource_id, observed_at, actor_id?, summary_json }` — bounded, no unbounded evidence |
| Success | 200 with `{ items: [...], next_cursor?: "..." }` |

### Section 5 tests

| Test ID | Scenario | Expected |
|---|---|---|
| TL-01 | Full lifecycle timeline | Events in `(timeline_sequence, id)` order |
| TL-02 | Cursor pagination coherent | No duplicates/gaps across pages; snapshot boundary holds |
| TL-03 | Event types coverage | All event types from vocabulary appear where applicable |
| TL-04 | No unbounded evidence in summaries | `summary_json` ≤ 2 KB, identifiers/hashes/scalars only |
| TL-05 | Replay/projection rebuild produces no timeline entry | Same-key retry, event dedup, projection rebuild |
| TL-06 | Cross-workspace isolation | Other workspace's timeline not visible |
| TL-07 | Schema-version dispatch | Legacy and schema-2 experiments both have timelines |
| TL-08 | Sequence allocation under lock | Two concurrent events get distinct sequences; no gap/duplicate |
| TL-09 | Lock order: class 7 after class 6 | Timeline lock acquired after Proposal lock |
| TL-10 | Legacy backfill deterministic | Backfilled events match `(observed_at ASC, id ASC)` order |
| TL-11 | Source-dedup unique prevents duplicate | Same `(source_resource_type, source_resource_id, event_type)` → no second row |
| TL-12 | Signed cursor tampered | 400 invalid cursor |
| TL-13 | Page-size out of bounds | `422 invalid_page_size` |
| TL-14 | Concurrent event after snapshot | Not visible in current cursor chain |
| TL-15 | Reversible eligibility event | No timeline entry emitted |

---

## A3. Cancel as a named mapping to close

**Classification: Approved mapping — no gap.**

Section 3 (§3.10) defines `POST /api/experiments/{id}/close` with `closure_kind` values that cover cancellation:

| Closure kind | State rule | Cancel semantic |
|---|---|---|
| `abandoned_before_start` | `frozen` only; no Judgement required | Cancel before start |
| `aborted` | `frozen` or `running`; no Judgement required | Operational cancel/abort |

Both are idempotent, `If-Match`-protected, and require `Idempotency-Key`. No separate `/cancel` route needed.

---

## A4. Replay taxonomy

**Classification: Approved mapping for 4 replay classes; notification coverage proven by §A5.**

### A4.1. Same-Idempotency-Key HTTP retry

**Approved** (§3.2): Same key + same fingerprint returns the original committed outcome before current state/authorization/eligibility guards. No re-authorization, no new side effects, no timeline entry. Same key + different fingerprint → `409 idempotency_key_reused`.

### A4.2. Identical judge evidence under a new Idempotency-Key

**Approved** (§3.8): Under a new key, full current guards are re-evaluated. Tenant-scoped Judgement uniqueness `UNIQUE(workspace_id, experiment_id, snapshot_hash, algorithm_version)` deduplicates: INSERT-or-load yields exactly one Judgement. Proposal uniqueness `UNIQUE(workspace_id, judgement_id, eligibility_context_hash)` deduplicates: if the `eligibility_context_hash` is identical, exactly one Proposal. If the eligibility context has changed, a new Proposal is created.

**Cardinality**: identical evidence + identical eligibility context + new key = exactly 1 Judgement + exactly 1 Proposal (INSERT-or-load, no timeline duplicate). Changed eligibility context = 1 Judgement + new Proposal (new timeline entry for the new Proposal only).

### A4.3. Upstream eligibility-event redelivery

**Approved** (§2.5): Partial unique indexes on `experiment_eligibility_events` deduplicate. Replay returns the existing event row; no duplicate append, no projection re-fold, no timeline entry.

### A4.4. Projection rebuild

**Approved** (§2.5): Replays events in deterministic `(observed_at ASC, id ASC)` order. Changes only projection state. Never modifies source events, Judgements, Proposals, Decisions, Confirmations, or timeline events.

### A4.5. Notification deduplication

Notification deduplication is proven by the append-only outbox (§A5) combined with the timeline source-dedup `UNIQUE(workspace_id, source_resource_type, source_resource_id, event_type)` constraint: no duplicate timeline event → no duplicate outbox row. AC-7 ("replays never duplicate notifications") is fully covered.

---

## A5. Experiment notification outbox

**Classification: Normative addition in complete design candidate — Section 2 + Section 4 extension.**

The existing `CustomerNotificationOutboxRecord` (in `app/entitlements/usage_models.py`) is guardrail/spend-specific. It cannot be safely generalized for experiment notifications. A separate experiment notification outbox is defined, anchored to timeline events.

### §2 extension: `ExperimentNotificationOutboxRecord`

Fully append-only. Issue #22 scope includes transactional durable outbox creation and replay deduplication. External transport dispatch (leases, retries, delivered/dead-letter state, delivery workers) is explicitly out of scope; a future issue must define the complete delivery state machine before external transport is enabled.

| Field | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | NOT NULL |
| `experiment_id` | String(64) | NOT NULL |
| `timeline_event_id` | String(64) | NOT NULL |
| `notification_type` | String(64) | NOT NULL; CHECK vocabulary below |
| `payload_canonical_json` | TEXT | NOT NULL; `CHECK(octet_length(payload_canonical_json) <= 4096)` (PG); `CHECK(length(CAST(payload_canonical_json AS BLOB)) <= 4096)` (SQLite) |
| `payload_hash` | String(64) | NOT NULL; SHA-256 of `payload_canonical_json` UTF-8 bytes |
| `payload_schema_version` | Integer | NOT NULL; CHECK IN (1) |
| `created_at` | DateTime(tz) | NOT NULL; server-assigned |
| | | Composite FK: `(workspace_id, timeline_event_id) → experiment_timeline_events` |
| | | `UNIQUE(workspace_id, timeline_event_id, notification_type)` |

**Payload canonicalization**: Service canonicalizes the payload to UTF-8 once using the shared canonical-JSON algorithm (§4.7). Service verifies parse + canonical round-trip and SHA-256 match before INSERT. PostgreSQL `octet_length` enforces UTF-8 byte length (not character count). Do not claim that PostgreSQL `json::text` equals service canonical bytes.

**Trigger model**: Fully append-only. Uses shared `marketai_reject_update_or_delete()` trigger. No mutable columns. DELETE rejected except during controlled workspace erasure.

**One-notification-per-domain-cause mapping:**

| Timeline event type | Notification type | Emitted? |
|---|---|---|
| `experiment_frozen` | `experiment_frozen` | Yes |
| `experiment_started` | `experiment_started` | Yes |
| `experiment_closed` | `experiment_closed` | Yes |
| `judgement_created` | `judgement_created` | Yes |
| `proposal_invalidated` | — | No (timeline event only; the upstream `eligibility_irreversible` already notified) |
| `confirmation_created` | `decision_confirmed` | Yes — the single notification for the confirmation + materialization domain cause |
| `decisions_materialized` | — | No (same-cause as confirmation; one notification per domain cause) |
| `eligibility_irreversible` | `eligibility_irreversible` | Yes — exactly ONE notification for the upstream cause; covers all downstream proposal invalidations |
| `experiment_created` | — | No notification |
| `proposal_created` | — | No notification |
| `next_round_created` | `next_round_created` | Yes |
| `pattern_candidate_created` | — | No notification |

For an irreversible eligibility event that invalidates multiple Proposals: the `eligibility_irreversible` timeline event produces exactly ONE user notification. Each resulting `proposal_invalidated` timeline event does NOT produce a notification. This prevents N+1 notifications for a single upstream cause. The `UNIQUE(workspace_id, timeline_event_id, notification_type)` constraint enforces at-most-one notification per timeline event.

For a confirmation transaction: the `confirmation_created` timeline event produces the `decision_confirmed` notification. The `decisions_materialized` timeline event does NOT produce a separate notification. This ensures one notification per domain cause (the user confirmed a decision), not two.

**Timeline events that NEVER notify**: `experiment_created`, `proposal_created`, `proposal_invalidated`, `decisions_materialized`, `pattern_candidate_created`, and any event type not in the mapping above.

**Transactional insertion**: The outbox row is inserted in the same database transaction as the timeline event. Rollback of the source event rolls back both timeline and outbox.

**Retention**: Outbox rows persist until controlled workspace erasure. No automatic purge or TTL in Issue #22 scope.

**Replay proofs:**

| Replay class | Notification behavior | Proof |
|---|---|---|
| Same-Idempotency-Key retry | No new timeline event → no outbox row | Idempotency replay returns before event/timeline creation |
| Identical judge (new key, same context) | INSERT-or-load Judgement/Proposal → outbox row only on INSERT winner | Timeline source-dedup unique prevents duplicate timeline entry → no duplicate outbox |
| Eligibility event redelivery | Partial-unique dedup → no new event → no timeline → no outbox | Event dedup prevents duplicate |
| Projection rebuild | No timeline event → no outbox | Rebuild does not insert timeline events |

### Section 5 tests

| Test ID | Scenario | Expected |
|---|---|---|
| NO-01 | Freeze produces timeline event + outbox row | Row with `experiment_frozen` type, correct dedup key |
| NO-02 | Same-key freeze retry | No duplicate outbox row (no duplicate timeline event) |
| NO-03 | Confirm produces two timeline events but only one outbox row | Transaction emits `confirmation_created` and `decisions_materialized` timeline events; exactly one `decision_confirmed` outbox row anchored to `confirmation_created`; zero outbox rows for `decisions_materialized` |
| NO-04 | Same-key confirm retry | No duplicate outbox row |
| NO-05 | Eligibility event replay | No timeline event → no outbox row |
| NO-06 | Projection rebuild | No outbox row |
| NO-07 | Confirmation rollback | Outbox row + timeline event rolled back with confirmation |
| NO-08 | `experiment_created` produces no outbox row | Timeline event exists; no notification |
| NO-09 | `proposal_created` produces no outbox row | Timeline event exists; no notification |
| NO-10 | Dedup: same timeline_event_id + notification_type | Unique violation |
| NO-11 | Cross-workspace isolation (RLS) | Other workspace's outbox not visible |
| NO-12 | Bounded payload | `payload_canonical_json` > 4096 bytes → service rejection + CHECK constraint (PG) |
| NO-13 | Append-only trigger: UPDATE rejected | Any UPDATE → trigger rejection |
| NO-14 | DELETE non-erasure | Rejected |
| NO-15 | Irreversible event: exactly one notification | `eligibility_irreversible` produces one outbox row; each `proposal_invalidated` timeline event produces NO outbox row; total notifications = 1 regardless of invalidated Proposal count |
| NO-16 | Invalid JSON or non-canonical bytes rejected | Parse + canonical round-trip verification rejects `payload_canonical_json` that is not valid JSON or whose canonical form differs from stored bytes |
| NO-17 | `payload_hash` mismatch rejected | Service rejects insert when `payload_hash` does not equal SHA-256 of stored `payload_canonical_json` |
| NO-18 | UTF-8 multibyte boundary: 4096 bytes accepted, 4097 rejected | PostgreSQL: `octet_length(payload_canonical_json) <= 4096` CHECK passes at 4096, fails at 4097. SQLite: `length(CAST(payload_canonical_json AS BLOB)) <= 4096` equivalent. Multibyte payload used to ensure byte vs. character divergence |

---

## A6. Next-round action

**Classification: Normative addition in complete design candidate — Section 3 + Section 4 extension.**

The existing `create_regeneration_draft(session, ctx, source_version_id, intent_kind, *, prompt_overrides=None, qc_attempt_id=None)` (in `app/generation/regeneration.py`) takes `source_version_id` (an AssetVersion ID) and resolves `source_asset_version_id` and `source_recipe_identity` internally. The caller does NOT pass those resolved fields. The function accepts `qc_attempt_id` but only checks for existence of any QC attempt; it does not persist the selected ID. Issue #22 requires an explicit, idempotent next-round action that does not invent fields on `GenerationDraft`.

### Contract

**Command**: `POST /api/experiments/{experiment_id}/decisions/{decision_id}/next-round`

Single-decision command. No unbounded `selected_decision_ids` array.

| Field | Value |
|---|---|
| Authorization | `experiment.next_round` permission; workspace membership |
| Precondition | Experiment `closed` with `closure_kind = completed`; the Decision is material (non-inconclusive: `win`, `learn`, or `lose`) and belongs to this Experiment/Version |
| `If-Match` | Experiment revision |
| `Idempotency-Key` | Required |
| Body | `{ "intent_kind": "same_recipe\|new_seed\|edit_prompt\|quality_feedback", "prompt_overrides": {...}?, "qc_attempt_id": "..."? }` — bounded, reuses approved regeneration validations |

**Action**: For the Decision's source Candidate's AssetVersion:

1. **Validate QC provenance** (Issue #22 wrapper responsibility):
   - If `intent_kind = 'quality_feedback'`: `qc_attempt_id` is required. Validate that the QC attempt belongs to the same workspace AND the same source AssetVersion as the Decision's Candidate. Reject cross-workspace or wrong-AssetVersion `qc_attempt_id`.
   - If `intent_kind ≠ 'quality_feedback'`: `qc_attempt_id` must be absent; reject if supplied.
2. Resolve `source_version_id` from the Decision's Candidate's `asset_version_id`.
3. Call `create_regeneration_draft(session, ctx, source_version_id=source_version_id, intent_kind=intent_kind, prompt_overrides=prompt_overrides, qc_attempt_id=qc_attempt_id)` — the function resolves `source_asset_version_id` and `source_recipe_identity` internally.
4. Create an immutable `ExperimentNextRoundRecord` linking the GenerationDraft back to the Experiment, persisting the validated `qc_attempt_id`.
5. Allocate timeline sequence and insert `next_round_created` timeline event.
6. Insert notification outbox row.
7. Commit atomically. Failure rolls back all records.

**`ExperimentNextRoundRecord`**:

| Field | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | NOT NULL |
| `experiment_id` | String(64) | NOT NULL |
| `experiment_version_id` | String(64) | NOT NULL |
| `decision_id` | String(64) | NOT NULL |
| `source_candidate_id` | String(64) | NOT NULL |
| `source_asset_version_id` | String(64) | NOT NULL |
| `generation_draft_id` | String(64) | NOT NULL; UNIQUE |
| `intent_kind` | String(32) | NOT NULL; CHECK IN (`same_recipe`, `new_seed`, `edit_prompt`, `quality_feedback`) |
| `qc_attempt_id` | String(64) | Nullable; CHECK: NOT NULL when `intent_kind = 'quality_feedback'`, NULL otherwise |
| `actor_id` | String(100) | NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL; server-assigned |

**Composite lineage FKs** (database-enforced identity):

| FK columns | Parent anchor |
|---|---|
| `(workspace_id, decision_id, experiment_id, experiment_version_id, source_candidate_id)` | `experiment_decisions(workspace_id, id, experiment_id, experiment_version_id, experiment_version_candidate_id)` — Decision lineage anchor (added by approved-section delta) |
| `(workspace_id, experiment_version_id, source_candidate_id, source_asset_version_id)` | `experiment_version_candidates(workspace_id, experiment_version_id, id, asset_version_id)` — Candidate lineage anchor (added by approved-section delta) |
| `(workspace_id, qc_attempt_id, source_asset_version_id)` | `asset_qc_attempts(workspace_id, id, asset_version_id)` — QC-attempt lineage anchor (added by approved-section delta); nullable FK (only when `qc_attempt_id IS NOT NULL`) |
| `(workspace_id, generation_draft_id)` | `generation_drafts(workspace_id, id)` — simple FK |

These composite FKs make wrong Decision/Version/Candidate/AssetVersion/QC-attempt combinations database failures, not only service checks. Service checks remain for semantic state/verdict/hash equalities that are not expressible as FK constraints.

- `generation_draft_id` UNIQUE — each draft links to at most one next-round association.
- No `UNIQUE(workspace_id, decision_id, intent_kind)` — multiple intentional rounds from the same Decision are allowed under different idempotency keys.
- No redundant idempotency data on the association record. Idempotency is handled by the HTTP-level `Idempotency-Key` mechanism.

**Invariants**:
- `learn` and `lose` Decisions are material and eligible for next-round (not only `win`).
- Judge, confirm, and close must NEVER trigger `create_regeneration_draft()`.
- No automatic GenerationDraft creation on any lifecycle event.
- No Library template mutation.

**Replay**: Same `Idempotency-Key` → `200` with original records. Different key on same Decision → may create another next-round (new draft, new association, new timeline event).

**Success**: `201` with the created `ExperimentNextRoundRecord` and linked `GenerationDraft`. `200` on same-key replay.

### Section 5 tests

| Test ID | Scenario | Expected |
|---|---|---|
| NR-01 | Valid next-round from completed experiment (win Decision) | 201; GenerationDraft + NextRoundRecord + timeline event |
| NR-02 | Valid next-round from learn Decision | 201; learn is material |
| NR-03 | Valid next-round from lose Decision | 201; lose is material |
| NR-04 | Non-completed experiment | 409 wrong state |
| NR-05 | Inconclusive Decision | 422 not material |
| NR-06 | Decision from different experiment | 404 |
| NR-07 | Idempotent replay (same key) | 200 with original records |
| NR-08 | Different key, same Decision | 201; new draft + association |
| NR-09 | Stale revision | 409 `revision_conflict` |
| NR-10 | No auto-draft on judge | Verify no GenerationDraft after judge |
| NR-11 | No auto-draft on confirm | Verify no GenerationDraft after confirm |
| NR-12 | No auto-draft on close | Verify no GenerationDraft after close |
| NR-13 | No Library template mutation | Library unchanged after next-round |
| NR-14 | Cross-workspace isolation | 404 for foreign workspace |
| NR-15 | Append-only trigger | UPDATE/DELETE on next_round_records rejected |
| NR-16 | Rollback: draft creation fails | No association, no timeline event, no outbox |
| NR-17 | generation_draft_id uniqueness | Second association with same draft_id → unique violation |
| NR-18 | quality_feedback requires qc_attempt_id | Missing → 422 |
| NR-19 | quality_feedback: valid qc_attempt_id | Same workspace, same AssetVersion → 201; `qc_attempt_id` persisted on record |
| NR-20 | quality_feedback: cross-workspace qc_attempt_id | Different workspace → 404/422 |
| NR-21 | quality_feedback: wrong AssetVersion qc_attempt_id | Same workspace, different AV → 422 |
| NR-22 | Non-quality_feedback with qc_attempt_id | `intent_kind = 'same_recipe'` + `qc_attempt_id` → 422 forbidden extra |
| NR-23 | qc_attempt_id CHECK constraint | `intent_kind = 'quality_feedback'` with NULL qc_attempt_id → CHECK violation |
| NR-24 | Decision belongs to stated Experiment/Version | Mismatched Decision → service rejection |
| NR-25 | source_asset_version_id equals Decision Candidate's AV | Verified at service layer |
| NR-26 | Decision lineage composite FK violation | Wrong experiment_version_id in Decision lineage → FK violation |
| NR-27 | Candidate/AssetVersion composite FK violation | Wrong source_candidate_id for stated experiment_version_id → FK violation |

---

## A7. Pattern candidate

**Classification: Normative addition in complete design candidate — Section 2 + Section 3 + Section 4 extension.**

The repository has no Pattern entity. Issue #22 explicitly scopes "produce next-round GenerationDraft/Pattern candidates only through explicit user action." The verdict projection is not a Pattern. The direction is Experiment → PatternCandidate, not Pattern → frozen candidate.

A Candidate may have multiple concepts (multi-concept). The pattern candidate must not collapse them into a single concept reference; it points to the whole source Candidate/Decision and commits a `source_concepts_hash` and `concept_count`.

**Cross-field lineage enforcement**: Composite FKs enforce tenant-scope and version-scope linkage at the database level. Same-transaction service checks enforce semantic state/verdict/hash equalities that FK constraints cannot express. Both FK violations and service-layer lineage mismatches must be tested.

### §2 extension: `ExperimentPatternCandidateRecord`

A minimal immutable record created only by an explicit command from a confirmed material Decision. It is NOT a Library template.

| Field | Type | Constraints |
|---|---|---|
| `id` | String(64) | PK |
| `workspace_id` | String(64) | NOT NULL |
| `experiment_id` | String(64) | NOT NULL |
| `experiment_version_id` | String(64) | NOT NULL |
| `decision_id` | String(64) | NOT NULL |
| `candidate_id` | String(64) | NOT NULL |
| `source_concepts_hash` | String(64) | NOT NULL; exact byte-for-byte copy of the source Candidate's persisted `concepts_hash` under the canonical algorithm defined by Sections 1/4 |
| `concept_count` | Integer | NOT NULL; CHECK > 0; must equal the source Candidate's concept-row count |
| `execution_mode` | String(16) | NOT NULL; CHECK IN (`byok`, `sandbox`, `mock`) |
| `verdict` | String(16) | NOT NULL; CHECK IN (`win`, `learn`, `lose`) — only material verdicts |
| `pattern_evidence_json` | JSON | NOT NULL; bounded ≤ 8 KB |
| `pattern_evidence_hash` | String(64) | NOT NULL |
| `actor_id` | String(100) | NOT NULL |
| `created_at` | DateTime(tz) | NOT NULL; server-assigned |
| | | `UNIQUE(workspace_id, decision_id)` — one pattern candidate per Decision; INSERT-or-load replay |

**Composite lineage FKs** (database-enforced identity):

| FK columns | Parent anchor |
|---|---|
| `(workspace_id, decision_id, experiment_id, experiment_version_id, candidate_id)` | `experiment_decisions(workspace_id, id, experiment_id, experiment_version_id, experiment_version_candidate_id)` — Decision lineage anchor (added by approved-section delta) |
| `(workspace_id, experiment_version_id, candidate_id)` | `experiment_version_candidates(workspace_id, experiment_version_id, id)` — Candidate version anchor (approved §4) |

### §3 extension: `POST /api/experiments/{experiment_id}/decisions/{decision_id}/pattern-candidates`

Single-decision command at the Decision resource.

| Field | Value |
|---|---|
| Authorization | `experiment.create_pattern` permission; workspace membership |
| Precondition | Experiment `closed` with `closure_kind = completed`; Decision is material (non-inconclusive) |
| `If-Match` | Experiment revision |
| `Idempotency-Key` | Required |
| Body | Empty or `{}` — all data derived from the Decision/Candidate lineage |
| Action | Create one `ExperimentPatternCandidateRecord` for the Decision; allocate timeline sequence; insert `pattern_candidate_created` event |
| Replay | Same Decision → INSERT-or-load returns existing record (`200`). UNIQUE(workspace_id, decision_id) enforces one per Decision. Different Idempotency-Key on same Decision returns the same existing record |
| Does NOT | Write to Library; create a template; mutate any existing record; trigger automatic creation |
| Success | 201 on first create; 200 on INSERT-or-load replay |
| `409 schema_version_mismatch` | Legacy experiment |

**Execution-mode isolation**: Discovery/promotion queries by downstream consumers MUST filter by `execution_mode` to separate mock/sandbox from production evidence. Issue #22 proves the field is present and correct; downstream consumption gates (#24/#25) are out of scope.

### Section 5 tests

| Test ID | Scenario | Expected |
|---|---|---|
| PC-01 | Create pattern from material win Decision | 201; record created with correct source_concepts_hash and concept_count |
| PC-02 | Create pattern from material learn Decision | 201; learn is material |
| PC-03 | Inconclusive Decision rejected | Service rejection (not material) |
| PC-04 | One pattern per Decision unique (INSERT-or-load) | Same Decision → 200 with existing record |
| PC-05 | Different Idempotency-Key, same Decision | 200 with existing record (INSERT-or-load, not idempotency-key replay) |
| PC-06 | Non-completed experiment | 409 wrong state |
| PC-07 | Evidence bounded | `pattern_evidence_json` > 8 KB → rejection |
| PC-08 | Append-only trigger | UPDATE/DELETE rejected |
| PC-09 | Cross-workspace isolation (RLS) | 404 for foreign workspace |
| PC-10 | Mock execution_mode carried | Record carries mock lineage from Candidate |
| PC-11 | No Library write | Library unchanged after pattern creation |
| PC-12 | Decision lineage composite FK enforcement | Mismatched workspace/experiment/version in Decision lineage → FK violation |
| PC-13 | Legacy experiment rejected | `409 schema_version_mismatch` |
| PC-14 | Timeline event emitted | `pattern_candidate_created` in timeline |
| PC-15 | No notification emitted | No outbox row for pattern creation |
| PC-16 | source_concepts_hash equals persisted Candidate hash | `source_concepts_hash` is exactly equal to the source Candidate's persisted `concepts_hash` — no recomputation |
| PC-17 | Multi-concept Candidate: canonical hash differentiates full content | concept_count > 1; canonical source hash covers all concept rows, not merely key tuples |
| PC-18 | Same keys, different frozen values → different persisted hash | Two Candidates with same `(dimension_key, concept_key_id)` but different frozen values → different persisted `concepts_hash` → different `source_concepts_hash` |
| PC-19 | candidate_id equals Decision's Candidate | Mismatched candidate_id (same workspace) → service rejection |
| PC-20 | Decision belongs to stated Experiment/Version | Mismatched lineage (same workspace) → service rejection |
| PC-21 | Decision lineage composite FK: wrong version | Decision from different experiment_version_id → FK violation |
| PC-22 | Candidate version composite FK: wrong version | candidate_id from different experiment_version_id → FK violation |

---

## A8. Localization/Scale unlock boundary

**Classification: Approved boundary (Issue #22 half) + no-unlock-side-effect contract.**

The approved model defines:
- `inconclusive` excluded from material Decisions (§2.5, §3.9 step h/l).
- Only `win`/`learn`/`lose` appear in `contextual_verdict_projections.verdict` (CHECK constraint, §4).
- `execution_mode` is on `experiment_version_candidates` and in the frozen Version; it is NOT a direct column on verdict projections. Downstream consumers obtain it by joining through the immutable ExperimentVersion lineage.

### No-unlock-side-effect contract

Issue #22 proves its half:
1. `inconclusive` never appears as `verdict` in `contextual_verdict_projections`.
2. `execution_mode` is traceable through immutable ExperimentVersion → Candidate lineage.
3. **No Localization or Scale unlock action is invoked or enqueued** by any Issue #22 code path (judge, confirm, close, next-round, pattern-create). This is verified by side-effect spies/stubs that assert zero calls to any Localization/Scale unlock interface.

Downstream Localization (#24) and Scale (#25) consumption gates are out of scope. They read verdict projections and enforce their own unlock rules.

### Section 5 tests

| Test ID | Scenario | Expected |
|---|---|---|
| UB-01 | Confirmation with win/learn/lose | No Localization/Scale unlock call |
| UB-02 | Confirmation with mock execution_mode | No Localization/Scale unlock call |
| UB-03 | Confirmation with sandbox execution_mode | No Localization/Scale unlock call |
| UB-04 | Close (completed) | No Localization/Scale unlock call |
| UB-05 | Pattern creation | No Localization/Scale unlock call |
| UB-06 | Next-round action | No Localization/Scale unlock call |

---

## A9. Candidate-count below-minimum HTTP status

**Classification: Normative addition in complete design candidate — Section 3 extension.**

Section 2 (§2.3, Gate 16) defines `insufficient_candidates_after_resolution` for surviving count < 2, and `candidate_limit_exceeded` for count > 100. Section 3 (§3.2) assigns `409` to `candidate_limit_exceeded` but does not explicitly assign an HTTP status to the below-minimum code.

### §3.2 extension

| Error code | HTTP status | Trigger |
|---|---|---|
| `insufficient_candidates_after_resolution` | 409 | Gate 16: surviving candidate count < 2 after resolution |
| `candidate_limit_exceeded` | 409 | Gate 16: candidate count > 100; or add-entry exceeds cap |

Both are 409 because the conflict is between the current experiment state and the freeze precondition. This aligns with the existing §3.2 pattern where precondition conflicts return 409.

---

## A10. Addendum table integration

All new addendum tables follow the approved §4 patterns for composite workspace FKs, RLS, FORCE RLS, grants, triggers, erasure, and migration. This section specifies the exact integration for each new table.

### A10.1 Tables and trigger model

| Table | Trigger model | Identity columns (immutable) | Mutable columns |
|---|---|---|---|
| `experiment_timeline_heads` | Mutable (UPDATE on `head_sequence` allowed) | `id`, `workspace_id`, `experiment_id` | `head_sequence` |
| `experiment_timeline_events` | Append-only | All columns | None |
| `experiment_notification_outbox` | Append-only | All columns | None |
| `experiment_next_round_records` | Append-only | All columns | None |
| `experiment_pattern_candidates` | Append-only | All columns | None |

### A10.2 RLS and FORCE RLS

Every new table:
- `ALTER TABLE ... ENABLE ROW LEVEL SECURITY`
- `ALTER TABLE ... FORCE ROW LEVEL SECURITY`
- Policy: `USING (marketai_context_is_current(workspace_id)) WITH CHECK (marketai_context_is_current(workspace_id))`

### A10.3 Role grants

Per the §4.9 pattern. API and Worker are listed separately. Denied operations are explicit.

**Runtime role grants:**

| Table | `marketai_domain_api` | `marketai_domain_worker` | `marketai_projection_writer` | `marketai_domain_readonly` |
|---|---|---|---|---|
| `experiment_timeline_heads` | SELECT, INSERT, UPDATE | SELECT, INSERT | SELECT | SELECT |
| `experiment_timeline_events` | SELECT, INSERT | SELECT, INSERT | SELECT | SELECT |
| `experiment_notification_outbox` | SELECT, INSERT | SELECT, INSERT | SELECT | SELECT |
| `experiment_next_round_records` | SELECT, INSERT | SELECT, INSERT | SELECT | SELECT |
| `experiment_pattern_candidates` | SELECT, INSERT | SELECT, INSERT | SELECT | SELECT |

**Explicit denials:**

| Role | Denied operations |
|---|---|
| `marketai_domain_api` | UPDATE on all append-only tables; DELETE on all addendum tables |
| `marketai_domain_worker` | UPDATE on all addendum tables; DELETE on all addendum tables |
| `marketai_projection_writer` | INSERT, UPDATE, DELETE on all addendum tables (SELECT only) |
| `marketai_domain_readonly` | INSERT, UPDATE, DELETE on all tables (SELECT only) |

**Erasure roles:**

| Role | Capabilities |
|---|---|
| `marketai_erasure_principal` (caller) | EXECUTE on the erasure SECURITY DEFINER procedure only. Zero direct table DML grants. No BYPASSRLS. |
| Designated admin (procedure OWNER) | BYPASSRLS. Internal DML required by the erasure procedure. Not a runtime membership target. `PUBLIC EXECUTE` on the erasure procedure is revoked. |

**Cross-role isolation:**
- `marketai_domain_api`, `marketai_domain_worker`, `marketai_domain_readonly`, and `marketai_projection_writer` cannot invoke the erasure procedure.
- No runtime role is a member of `marketai_erasure_principal`.
- No runtime role is a member of the designated admin owner role.

### A10.4 Erasure dependency order

Extended dependency order for workspace erasure (leaf → root):

1. `experiment_pattern_candidates` (leaf — depends on experiments, versions, decisions, candidates)
2. `experiment_next_round_records` (depends on experiments, versions, decisions, asset_versions, generation_drafts, optionally qc_attempts)
3. `experiment_notification_outbox` (depends on timeline_events)
4. `experiment_timeline_events` (depends on experiments)
5. `experiment_timeline_heads` (depends on experiments)
6. ... (existing experiment evidence tables per §4)
7. ... (existing experiments, workspaces)

The erasure procedure deletes in this order. RESTRICT FKs block misordered deletion.

### A10.5 Migration creation/drop order

**Upgrade**: Create tables in root → leaf order:
1. `experiment_timeline_heads` (depends on experiments)
2. `experiment_timeline_events` (depends on experiments)
3. `experiment_notification_outbox` (depends on timeline_events)
4. `experiment_next_round_records` (depends on experiments, experiment_versions, decisions, asset_versions, generation_drafts, experiment_version_candidates, asset_qc_attempts)
5. `experiment_pattern_candidates` (depends on experiments, experiment_versions, decisions, experiment_version_candidates)

**Downgrade**: Drop in leaf → root order (reverse of creation).

### A10.6 Downgrade preflight

Before dropping addendum tables:
- Abort if any `experiment_next_round_records` rows exist (non-re-derivable data).
- Abort if any `experiment_pattern_candidates` rows exist (non-re-derivable data).
- Timeline events and notification outbox are re-derivable (can be recomputed from source events); may be dropped.
- Timeline heads are re-derivable; may be dropped.

### A10.7 SQLite guards

On SQLite, where triggers are unavailable:
- Append-only enforcement: service-level rejection of UPDATE/DELETE.
- RLS: service-level workspace scoping.
- Erasure: service-level `is_erasure_principal` flag + dependency ordering.
- Payload byte-length: `CHECK(length(CAST(payload_canonical_json AS BLOB)) <= 4096)` at DB level; service-level verification as primary guard.

### A10.8 Acceptance tests for addendum tables

Each addendum table must pass the following tests. Each test has a stable ID (`ATI-NN`) for traceability into Section 5.

| Test ID | Test category | Per table | Layer |
|---|---|---|---|
| ATI-01 | RLS ENABLE + FORCE | `pg_class.relrowsecurity` and `relforcerowsecurity` = true | Integration-PG |
| ATI-02 | RLS policy exists | `USING` and `WITH CHECK` with `marketai_context_is_current` | Integration-PG |
| ATI-03 | Cross-workspace SELECT | Empty result with foreign workspace context | Integration-PG |
| ATI-04 | Cross-workspace INSERT | RLS policy violation | Integration-PG |
| ATI-05 | Composite FK | Mismatched workspace_id → FK violation | Integration-PG |
| ATI-06 | Append-only trigger (where applicable) | UPDATE/DELETE → trigger rejection (via DML-capable role) | Integration-PG |
| ATI-07 | *(removed — outbox is now fully append-only; covered by ATI-06)* | — | — |
| ATI-08 | Grant: API SELECT, INSERT | Allowed | Integration-PG |
| ATI-09 | Grant: API UPDATE on append-only | Denied | Integration-PG |
| ATI-10 | Grant: API DELETE on append-only | Denied | Integration-PG |
| ATI-11 | Grant: Worker SELECT, INSERT | Allowed | Integration-PG |
| ATI-12 | *(removed — no delivery columns)* | — | — |
| ATI-13 | *(removed — no delivery columns)* | — | — |
| ATI-14 | Grant: Readonly SELECT | Allowed | Integration-PG |
| ATI-15 | Grant: Readonly INSERT/UPDATE/DELETE | Denied | Integration-PG |
| ATI-16 | Erasure: procedure deletes in order | All rows deleted | Integration-PG |
| ATI-17 | Erasure: misordered | RESTRICT FK blocks | Integration-PG |
| ATI-18 | SQLite append-only guard | Service rejects UPDATE/DELETE | Domain |
| ATI-19 | SQLite workspace scoping | Only own workspace visible | Domain |
| ATI-20 | Erasure caller: no direct DML on any experiment-domain table | `marketai_erasure_principal` SELECT/INSERT/UPDATE/DELETE → denied on every experiment-domain table (approved + addendum) | Integration-PG |
| ATI-21 | Procedure owner is not a runtime membership target | `pg_has_role(runtime_role, owner_role, 'member')` = false for API/Worker/Readonly/ProjectionWriter | Integration-PG |
| ATI-22 | API/Worker cannot invoke erasure procedure | EXECUTE → denied | Integration-PG |
| ATI-23 | ProjectionWriter: SELECT only on addendum tables | INSERT/UPDATE/DELETE → denied | Integration-PG |
| ATI-24 | NextRound Decision lineage FK violation | Wrong experiment_version_id in Decision lineage → FK violation | Integration-PG |
| ATI-25 | NextRound Candidate/AssetVersion FK violation | Wrong source_candidate_id for stated experiment_version_id → FK violation | Integration-PG |
| ATI-26 | NextRound QC-attempt FK violation | Wrong source_asset_version_id for stated qc_attempt_id → FK violation | Integration-PG |
| ATI-27 | PatternCandidate Decision lineage FK violation | Wrong experiment_version_id in Decision lineage → FK violation | Integration-PG |
| ATI-28 | PatternCandidate Candidate version FK violation | Wrong experiment_version_id for stated candidate_id → FK violation | Integration-PG |
| ATI-29 | Grant: API UPDATE on timeline_heads | UPDATE `head_sequence` allowed for `marketai_domain_api` | Integration-PG |
| ATI-30 | Grant: Worker UPDATE on all addendum tables | Denied (Worker has SELECT + INSERT only) | Integration-PG |
| ATI-31 | Grant: Worker DELETE on all addendum tables | Denied | Integration-PG |
| ATI-32 | Erasure caller: SELECT on every experiment-domain table | Denied (EXECUTE only, zero DML on approved + addendum tables) | Integration-PG |
| ATI-33 | Erasure caller: no BYPASSRLS | `pg_roles.rolbypassrls` = false for `marketai_erasure_principal` | Integration-PG |
| ATI-34 | Erasure procedure owner: BYPASSRLS present | `pg_roles.rolbypassrls` = true for designated admin | Integration-PG |
| ATI-35 | Erasure procedure: call as erasure_principal succeeds | `marketai_erasure_principal` EXECUTE succeeds; owner-rights internal DML deletes only target workspace rows | Integration-PG |
| ATI-36 | PUBLIC EXECUTE revoked on erasure procedure | `SELECT has_function_privilege('public', ...)` = false | Integration-PG |
| ATI-37 | Readonly EXECUTE on erasure procedure | Denied | Integration-PG |
| ATI-38 | ProjectionWriter EXECUTE on erasure procedure | Denied | Integration-PG |
| ATI-39 | No runtime role is a member of erasure principal | `pg_has_role(role, 'marketai_erasure_principal', 'member')` = false for API/Worker/Readonly/ProjectionWriter | Integration-PG |
| ATI-40 | ProjectionWriter SELECT succeeds on every addendum table | SELECT returns rows (or empty set) without privilege error | Integration-PG |
| ATI-41 | Erasure caller: no EXECUTE on any other SECURITY DEFINER function | `marketai_erasure_principal` EXECUTE on projection function and any other SECDEF → denied | Integration-PG |

---

## A11. Design status

All items in this addendum are normative parts of the complete design candidate.

| Item | Classification | Status |
|---|---|---|
| A1. List endpoint | Normative — Section 3 extension | Included |
| A2. Timeline endpoint | Normative — Section 2 + 3 + 4 extension | Included |
| A3. Cancel mapping | Approved mapping | No gap |
| A4. Replay taxonomy | Approved (4 classes) | No gap |
| A5. Notification outbox | Normative — Section 2 + 4 extension | Included |
| A6. Next-round action | Normative — Section 3 + 4 extension | Included |
| A7. Pattern candidate | Normative — Section 2 + 3 + 4 extension | Included |
| A8. Unlock boundary | Normative — side-effect contract | Included |
| A9. Below-minimum status | Normative — Section 3 extension | Included |
| A10. Table integration | Normative — Section 4 extension | Included |

**`design_complete = true`**: All mandatory design gaps are closed. Zero unresolved product-semantic decisions. Zero known design gaps.

**`implementation_ready = true`**: Holistic final-design approval granted 2026-09-03. Complete design SHA-256: `1210427eb9f6b479b84f167d435c7c354785c1f4c9b1d6a3b60f49245f902f8e`.

Explicit Issue #22 acceptance scope cannot be deferred while claiming design completeness. Removal of scope requires a formal amendment of Issue #22 with explicit approval of that amendment; naming a future issue alone does not satisfy this requirement.


<!-- source-boundary: section5-draft -->
# Section 5: Automated Evidence, Verification Strategy, and Acceptance Traceability

This section defines the executable test contract for Issue #22. It provides two traceability matrices: one for the seven GitHub Issue #22 acceptance criteria (§5.2) and one for the 88 §4.11 migration checks (§5.13). It defines fixture infrastructure, test vectors, domain scenarios, integration proofs, and CI gates required to declare "done."

Sections 1–4 are approved constraints. This section does not redesign them. Genuine unresolved design dependencies are registered in §5.0 and reconciled in the cross-section addendum (`.claude/issue22-cross-section-addendum-draft.md`).

---

## §5.0 Coverage-gap register

Issue #22 scope items mapped to approved contracts or normative additions in the complete design candidate. Section 5 may not invent domain behavior and call it approved.

| Scope item | Status | Approved contract | Addendum reference |
|---|---|---|---|
| List API | **Normative addition** | `GET /api/experiments` extends approved §3.12 | §A1 |
| Detail API | **Approved** | §3.1 detail endpoint approved | — |
| Timeline API | **Normative addition** | Extends approved §3.12 | §A2 |
| Cancel API | **Approved mapping** | `POST /api/experiments/{id}/close` with `abandoned_before_start` or `aborted` (§3.10) | §A3 |
| Replay taxonomy (4 classes) | **Approved mapping** | §3.2 idempotency, §3.8 judge uniqueness, §2.5 event dedup, §2.5 projection rebuild | §A4 |
| Notification deduplication | **Normative addition** | Append-only experiment notification outbox | §A5 |
| Next-round GenerationDraft | **Normative addition** | Explicit command with immutable association and composite lineage FKs | §A6 |
| Pattern candidate identity | **Normative addition** | `ExperimentPatternCandidateRecord` with composite lineage FKs | §A7 |
| Localization/Scale unlock boundary | **Normative addition** | No-unlock-side-effect contract | §A8 |
| Candidate count below-minimum HTTP status | **Normative addition** | `insufficient_candidates_after_resolution` → 409 | §A9 |
| Addendum table integration (RLS/grants/triggers/erasure/migration) | **Normative addition** | Full §4 integration for addendum tables | §A10 |
| Optimistic revisions | **Approved** | §3.2 `If-Match`, §2.1 experiment revision, §2.5 proposal sequence CAS | — |
| Reason codes and evidence snapshots | **Approved** | §2.3 gate evidence, §2.5 eligibility evidence, §3.10 close reason | — |

**Consequence**: `design_complete = true`. `implementation_ready = true` — holistic final-design approval granted 2026-09-03. Complete design SHA-256: `1210427eb9f6b479b84f167d435c7c354785c1f4c9b1d6a3b60f49245f902f8e` (5,471 lines). There are zero unresolved product-semantic decisions and zero known design gaps.

---

## §5.1 Test infrastructure and fixture contracts

### §5.1.1 Test layers

| Layer | Scope | Database | Framework | Determinism |
|---|---|---|---|---|
| **Unit** | Pure functions, no I/O | None | pytest | Fully deterministic; fixed seeds |
| **Property** | Generative/fuzz over pure functions | None | pytest + Hypothesis | Seed-reproducible |
| **Domain** | Service-level behavior with real DB | SQLite (in-memory) or PostgreSQL (test instance) | pytest | Deterministic clock; ordered IDs |
| **Integration-PG** | PostgreSQL-only DDL/trigger/RLS/grant/concurrency | PostgreSQL 16 test instance | pytest + psycopg | Real row locks; no SQLite fallback |
| **Migration** | Alembic upgrade/downgrade lifecycle | Both dialects | pytest + Alembic test harness | Clean DB per test; idempotent assertions |
| **API** | HTTP endpoint contract | PostgreSQL (test instance) | pytest + httpx/TestClient | Deterministic clock; test tenant |

### §5.1.2 Shared fixtures

**`deterministic_clock`** — injectable clock returning strictly monotonic `datetime(tz=UTC)`. Advances by 1 microsecond per call. Used for all server-assigned timestamps (`observed_at`, `created_at`, `confirmed_at`). Prevents wall-clock non-determinism.

**`test_workspace`** — creates a Workspace with `workspace_id = "ws-test-001"`. On PostgreSQL, sets three signed-context GUCs via psycopg:
- `SET marketai.workspace_id = 'ws-test-001'`
- `SET marketai.authorization_epoch = '<current epoch>'`
- `SET marketai.context_signature = '<HMAC-SHA256 of (workspace_id, authorization_epoch)>'`

The function `marketai_context_is_current('ws-test-001')` must return `true` after this setup. On SQLite, the equivalent session context is set by the service layer. All tenant-scoped operations use this workspace.

**`second_workspace`** — `workspace_id = "ws-test-002"` with its own valid signed context. Used exclusively for cross-workspace negative tests. `marketai_context_is_current('ws-test-002')` returns `true` only when this workspace's GUCs are active.

**`pg_roles`** — PostgreSQL fixture that creates test connections as each role: `marketai_domain_api`, `marketai_domain_worker`, `marketai_projection_writer`, `marketai_erasure_principal`, `marketai_domain_readonly`. Each connection has the role's grants and the test workspace signed context set via psycopg (sync).

**`schema2_experiment`** — creates a schema-2 Experiment with `record_schema_version=2`, two entries, a valid product/offer in the library, truth-dependency set, QC passed, and effective review `approved`. Returns the Experiment with its revision.

**`frozen_experiment`** — extends `schema2_experiment`: freezes with a valid resolution plan, producing an ExperimentVersion with 2+ candidates and all integrity projections verified. Returns the frozen Experiment, Version, and Candidates.

**`running_experiment`** — extends `frozen_experiment`: binds candidates and starts the experiment. Returns the running Experiment with bindings.

**`judged_experiment`** — extends `running_experiment`: runs a judgement producing a Judgement, Proposal, and eligibility projections. Returns the full chain.

**`confirmed_experiment`** — extends `judged_experiment`: confirms the proposal producing a Confirmation, material Decisions, and verdict projections. Returns the complete decision chain.

**`canonical_test_vector`** — a fixed nested object containing: tz-aware datetimes (UTC and offset), `Decimal("1.230000")`, `Decimal("1000")`, `Decimal("-0")`, `Decimal("0.001")`, nested dicts with sorted keys, lists, `None`, integers, and strings. The canonical JSON output and SHA-256 hash are pinned literals in the test.

**`legacy_experiment`** — creates a legacy Experiment with `record_schema_version=NULL`, legacy entries, legacy judgements and decisions with `decision_schema_version=NULL`. Used for coexistence and discrimination tests.

**`judge_v1_fixtures`** — legacy judge input/output pairs for regression. Pinned snapshot → pinned verdicts.

**`judge_v2_fixtures`** — schema-2 candidate-keyed judge input/output pairs. Pinned snapshot → pinned verdicts across multiple evidence configurations (all-win, mixed, all-inconclusive).

### §5.1.3 Deterministic ID generation

All test IDs are generated by a seeded `uuid4`-like function producing deterministic, ordered IDs. Format: `test-{entity}-{sequence:04d}`. This ensures `ORDER BY id` is deterministic and cross-dialect identical.

### §5.1.4 Test database lifecycle

- **PostgreSQL**: each test module gets a fresh database created from the migration head. Roles and grants are applied once per session. Each test function runs in a transaction that rolls back (except migration lifecycle tests, which commit).
- **SQLite**: in-memory database per test function. Migration applied at function scope.

### §5.1.5 Pytest markers (implementation-plan addition)

If new markers are required to separate PostgreSQL-only tests from default tests, they are declared as implementation-plan changes, not assumed to exist:

- `@pytest.mark.pg` — requires `MARKETAI_PG_TEST_URL` and role fixtures; fails (not skips) when absent in the `domain-rls` CI job.
- `@pytest.mark.migration` — migration lifecycle tests.

These markers, if adopted, must be registered in `pyproject.toml` or `pytest.ini`.

---

## §5.2 Issue #22 acceptance-criterion traceability

This matrix covers the seven GitHub Issue #22 acceptance criteria. Each criterion maps to specific test IDs, fixture, command/endpoint, expected persisted and absent side effects, layer, and failure diagnostic.

### AC-1: Draft edits allowed only before freeze; frozen inputs and hashes immutable

| Test ID | Scenario | Fixture | Command | Expected persisted | Expected absent | Layer | Failure diagnostic |
|---|---|---|---|---|---|---|---|
| AC1-01 | Add entry to draft | schema2_experiment | `POST /api/experiments/{id}/entries` | Entry created, revision incremented | — | Domain | Entry not created |
| AC1-02 | Update entry in draft | schema2_experiment | `PUT /api/experiments/{id}/entries/{eid}` | Entry updated, revision incremented | — | Domain | Entry not updated |
| AC1-03 | Remove entry from draft | schema2_experiment | `DELETE /api/experiments/{id}/entries/{eid}` | Entry removed, revision incremented | — | Domain | Entry not removed |
| AC1-04 | Update context in draft | schema2_experiment | `PUT /api/experiments/{id}/context` | Context updated, revision incremented | — | Domain | Context not updated |
| AC1-05 | Add entry after freeze | frozen_experiment | `POST /api/experiments/{id}/entries` | — | No entry, no revision change | Domain | Draft edit accepted after freeze |
| AC1-06 | Update entry after freeze | frozen_experiment | `PUT /api/experiments/{id}/entries/{eid}` | — | No entry mutation | Domain | Draft edit accepted after freeze |
| AC1-07 | Remove entry after freeze | frozen_experiment | `DELETE /api/experiments/{id}/entries/{eid}` | — | No entry removal | Domain | Draft edit accepted after freeze |
| AC1-08 | Update context after freeze | frozen_experiment | `PUT /api/experiments/{id}/context` | — | No context mutation | Domain | Draft edit accepted after freeze |
| AC1-09 | Frozen snapshot hash immutable (service) | frozen_experiment | Attempt service-level update of `frozen_snapshot_json` | — | Hash unchanged | Domain | Snapshot mutated |
| AC1-10 | Frozen snapshot hash immutable (SQL UPDATE) | frozen_experiment | Direct `UPDATE experiment_versions SET frozen_snapshot_json = ...` | Trigger rejection | No mutation | Integration-PG | Append-only trigger bypassed |
| AC1-11 | Frozen snapshot hash immutable (SQL DELETE) | frozen_experiment | Direct `DELETE FROM experiment_versions WHERE ...` | Trigger rejection | No deletion | Integration-PG | Append-only trigger bypassed |
| AC1-12 | Candidate hash immutable (SQL UPDATE) | frozen_experiment | Direct `UPDATE experiment_version_candidates SET ...` | Trigger rejection | No mutation | Integration-PG | Append-only trigger bypassed |
| AC1-13 | Frozen contract hashes verified at insert | frozen_experiment | Verify `frozen_snapshot_hash = _sha256(_canonical_bytes(frozen_snapshot_json))` | Equality holds | — | Unit + Domain | Hash/snapshot divergence |
| AC1-14 | Normalized integrity projections match snapshot | frozen_experiment | Verify all projection columns equal canonical-decoded snapshot values | Equality holds | — | Unit + Domain | Projection/snapshot divergence |

### AC-2: Non-Approved, withdrawn, stale-truth, mixed-mode, cross-Workspace cannot enter frozen set

| Test ID | Scenario | Fixture | Command | Expected persisted | Expected absent | Layer | Failure diagnostic |
|---|---|---|---|---|---|---|---|
| AC2-01 | Non-approved review at freeze | schema2_experiment (review=rejected) | `POST /api/experiments/{id}/freeze` | — | No Version created | Domain | Gate 1 did not fail-closed |
| AC2-02 | No review exists at freeze | schema2_experiment (no review) | `POST /api/experiments/{id}/freeze` | — | No Version created | Domain | Gate 1 did not fail-closed on missing review |
| AC2-03 | NULL review_value at freeze | schema2_experiment (review_value=NULL) | `POST /api/experiments/{id}/freeze` | — | No Version created | Domain | Gate 1 did not fail-closed on NULL |
| AC2-04 | Withdrawn ProductVersion at freeze | schema2_experiment (withdrawn PV) | `POST /api/experiments/{id}/freeze` | — | No Version created | Domain | Gate 14 did not reject |
| AC2-05 | Stale truth at freeze (evaluable but outdated) | schema2_experiment (stale truth, evaluable) | `POST /api/experiments/{id}/freeze` | — | No Version created; freeze result = `stale_truth` | Domain | Gate 13 did not reject stale truth |
| AC2-05a | Unevaluable truth at freeze (evidence/model absent) | schema2_experiment (unevaluable truth) | `POST /api/experiments/{id}/freeze` | — | No Version created; freeze result = `stale_truth_unevaluable` | Domain | Gate 13 did not reject unevaluable truth |
| AC2-06 | Mixed execution_mode at freeze | schema2_experiment (mixed mock+byok) | `POST /api/experiments/{id}/freeze` | — | No Version created | Domain | Gate 12 did not reject |
| AC2-07 | Cross-workspace version FK | frozen_experiment + second_workspace | Direct SQL INSERT with mismatched workspace_id | FK violation | No cross-workspace row | Integration-PG | FK did not enforce |
| AC2-08 | Advisory warnings at draft creation | schema2_experiment (review=rejected) | `POST /api/experiments` | Experiment created (draft state); warnings in response | No freeze, no Version | Domain | Advisory gates blocked creation or failed silently |
| AC2-09 | Gate 1 exact total order | schema2_experiment (approved then rejected) | `POST /api/experiments/{id}/freeze` | — | No Version (latest is rejected) | Domain | Total order `(created_at DESC, id DESC)` broken |
| AC2-10 | Gate 1 legacy_unscoped exclusion | schema2_experiment (legacy_unscoped=true review) | `POST /api/experiments/{id}/freeze` | — | Legacy review excluded from effective set | Domain | legacy_unscoped not filtered |
| AC2-11 | Gate 1 wrong AssetVersion exclusion | schema2_experiment (review for different AV) | `POST /api/experiments/{id}/freeze` | — | Wrong-AV review excluded | Domain | AV scope not enforced |
| AC2-12 | Cross-workspace AV via service: add-entry | schema2_experiment + second_workspace AV | `add_entry()` with foreign-workspace AssetVersion | — | No Entry created | Domain | Cross-workspace AV accepted in entry |
| AC2-13 | Cross-workspace AV via service: freeze | schema2_experiment + second_workspace AV | `freeze_experiment()` with foreign-workspace AssetVersion candidate | — | No Version, no Candidate, no event | Domain | Cross-workspace AV survived freeze |
| AC2-14 | Cross-workspace AV via API: add-entry | schema2_experiment + second_workspace AV | `POST /api/experiments/{id}/entries` with foreign-workspace AV | 404 | No Entry created | API | Cross-workspace AV accepted via API |

### AC-3: Same-concept duplication warns and requires explicit resolution before freeze

| Test ID | Scenario | Fixture | Command | Expected persisted | Expected absent | Layer | Failure diagnostic |
|---|---|---|---|---|---|---|---|
| AC3-01 | Duplicate concept detected, no resolution | schema2_experiment (duplicate group) | `POST /api/experiments/{id}/freeze` | — | No Version created | Domain | Gate 15 did not reject |
| AC3-02 | allow_controlled_duplicate | schema2_experiment (resolution plan) | `POST /api/experiments/{id}/freeze` | Version with both candidates; resolution record | — | Domain | Resolution not applied |
| AC3-03 | exclude_source_candidate | schema2_experiment (exclude plan) | `POST /api/experiments/{id}/freeze` | Version with excluded candidate removed; surviving count ≥ 2 | Excluded candidate not in Version | Domain | Exclusion not applied |
| AC3-04 | remap_before_freeze | schema2_experiment (remap plan) | `POST /api/experiments/{id}/freeze` | Version with remapped concept; new duplicate_group_hash | — | Domain | Remap not applied |
| AC3-05 | Malformed resolution plan | schema2_experiment (invalid plan) | `POST /api/experiments/{id}/freeze` | — | No Version created | Domain | Invalid plan accepted |
| AC3-06 | Stale resolution plan (revision changed) | schema2_experiment (stale plan) | `POST /api/experiments/{id}/freeze` | 409 `revision_conflict` | No Version created | Domain | Stale plan accepted |
| AC3-07 | Incomplete resolution (unresolved group) | schema2_experiment (partial plan) | `POST /api/experiments/{id}/freeze` | — | No Version created | Domain | Gate 15 did not reject |
| AC3-08 | duplicate_group_hash NOT NULL on every concept | frozen_experiment | Post-freeze verification | Every concept row has non-NULL `duplicate_group_hash` | — | Domain | NULL duplicate_group_hash |
| AC3-09 | Deterministic duplicate_group_hash | schema2_experiment | Compute hash from `(dimension_key, concept_key_id)` | Matches stored value | — | Unit | Hash computation divergence |

### AC-4: Pure judge deterministic; DB/time/environment changes do not affect identical evidence

| Test ID | Scenario | Fixture | Command | Expected persisted | Expected absent | Layer | Failure diagnostic |
|---|---|---|---|---|---|---|---|
| AC4-01 | judge-v1 regression | judge_v1_fixtures | `judge(snapshot)` | Identical verdicts to pinned output | — | Unit | Legacy judge regression |
| AC4-02 | judge-v2 regression | judge_v2_fixtures | `judge(snapshot)` | Identical verdicts to pinned output | — | Unit | Schema-2 judge regression |
| AC4-03 | Same evidence, different wall clock | judge_v2_fixtures | `judge(snapshot)` at t=0 and t=1hr | Identical verdicts | — | Unit | Clock affected output |
| AC4-04 | Same evidence, different timezone | judge_v2_fixtures | `judge(snapshot)` in UTC and UTC+8 | Identical verdicts | — | Unit | Timezone affected output |
| AC4-05 | Same evidence, different locale | judge_v2_fixtures | `judge(snapshot)` in en_US and zh_CN | Identical verdicts | — | Unit | Locale affected output |
| AC4-06 | Same evidence, different env vars | judge_v2_fixtures | `judge(snapshot)` with/without env vars | Identical verdicts | — | Unit | Env vars affected output |
| AC4-07 | Same evidence, reversed candidate ordering in snapshot | judge_v2_fixtures | `judge(snapshot)` with reversed candidate key order | Identical verdicts | — | Unit | Snapshot ordering affected output |
| AC4-08 | Repeated calls, same evidence | judge_v2_fixtures | `judge(snapshot)` × 100 | All identical | — | Unit | Non-determinism detected |
| AC4-09 | judge does not read DB (source scan) | Code inspection | Verify `judge()` signature accepts only snapshot object | — | No DB session/connection parameter | Unit (source scan) | DB parameter found |
| AC4-10 | judge does not call clock (source scan) | Code inspection | Verify no `datetime.now()`, `time.time()`, or clock access | — | No clock calls | Unit (source scan) | Clock call found |
| AC4-11 | judge does not use random (source scan) | Code inspection | Verify no `random.*` or `secrets.*` calls | — | No random calls | Unit (source scan) | Random call found |
| AC4-12 | Dependency-denial: DB access poisoned | judge_v2_fixtures | Monkeypatch all DB/session/connection modules to raise on access; `judge(snapshot)` still produces correct result | Identical verdicts | No DB access attempt | Unit | Judge accessed DB |
| AC4-13 | Dependency-denial: clock poisoned | judge_v2_fixtures | Monkeypatch `datetime.now`, `time.time`, `time.monotonic` to raise; `judge(snapshot)` still produces correct result | Identical verdicts | No clock access | Unit | Judge accessed clock |
| AC4-14 | Dependency-denial: environment poisoned | judge_v2_fixtures | Monkeypatch `os.environ` to raise on any access; `judge(snapshot)` still produces correct result | Identical verdicts | No env access | Unit | Judge accessed env |
| AC4-15 | Dependency-denial: random poisoned | judge_v2_fixtures | Monkeypatch `random.Random`, `secrets.token_bytes` to raise; `judge(snapshot)` still produces correct result | Identical verdicts | No random access | Unit | Judge accessed random |

### AC-5: Judge appends Judgement/Proposal only; permitted actor confirms data-sufficient Proposal

| Test ID | Scenario | Fixture | Command | Expected persisted | Expected absent | Layer | Failure diagnostic |
|---|---|---|---|---|---|---|---|
| AC5-01 | Judge creates Judgement + Proposal | running_experiment | `POST /api/experiments/{id}/judge` | Judgement + Proposal + `created` state event | No Decision, no Confirmation, no verdict projection | Domain | Judge created forbidden record type |
| AC5-02 | Judge does not create Decision | running_experiment | `POST /api/experiments/{id}/judge` | — | No ExperimentDecisionRecord rows | Domain | Decision created by judge |
| AC5-03 | Judge does not create Confirmation | running_experiment | `POST /api/experiments/{id}/judge` | — | No DecisionConfirmation rows | Domain | Confirmation created by judge |
| AC5-04 | Judge does not create verdict projection | running_experiment | `POST /api/experiments/{id}/judge` | — | No contextual_verdict_projection rows | Domain | Projection created by judge |
| AC5-05 | Permitted actor confirms | judged_experiment | `POST /api/experiments/{id}/proposals/{pid}/confirm` | Confirmation + Decisions + projections | — | Domain | Confirmation rejected for permitted actor |
| AC5-06 | Non-permitted actor denied | judged_experiment (wrong permission) | `POST /api/experiments/{id}/proposals/{pid}/confirm` | — | No Confirmation | API | Unauthorized actor confirmed |
| AC5-07 | Wrong workspace membership denied | judged_experiment (second_workspace actor) | `POST /api/experiments/{id}/proposals/{pid}/confirm` | — | No Confirmation | API | Cross-workspace actor confirmed |
| AC5-08 | Authorization epoch stale | judged_experiment (stale epoch) | `POST /api/experiments/{id}/proposals/{pid}/confirm` | — | No Confirmation | Domain | Stale authorization accepted |
| AC5-09 | All-inconclusive blocks confirmation | judged_experiment (all inconclusive) | `POST /api/experiments/{id}/proposals/{pid}/confirm` | — | No Confirmation | Domain | Inconclusive proposal confirmed |
| AC5-10 | Confirmation persists actor_id and confirmed_at | confirmed_experiment | Verify Decision rows | `actor_id` = Confirmation's; `confirmed_at` = Confirmation's | — | Domain | Actor/time mismatch |
| AC5-11 | Confirmation transaction rollback | judged_experiment (inject failure) | `POST /api/experiments/{id}/proposals/{pid}/confirm` with injected Decision write failure | — | No Confirmation, no Decisions, no projections, no outbox | Domain | Partial commit survived |

### AC-6: Inconclusive never material verdict; cannot unlock Localization or Scale

| Test ID | Scenario | Fixture | Command | Expected persisted | Expected absent | Layer | Failure diagnostic |
|---|---|---|---|---|---|---|---|
| AC6-01 | All-inconclusive blocks confirmation | judged_experiment (all inconclusive) | `POST /api/experiments/{id}/proposals/{pid}/confirm` | 409 `no_material_verdicts` | No Decision, no Confirmation | Domain | Inconclusive became material |
| AC6-02 | Mixed verdicts: inconclusive excluded | judged_experiment (win+inconclusive) | `POST /api/experiments/{id}/proposals/{pid}/confirm` | Decisions only for non-inconclusive candidates | No Decision for inconclusive candidate | Domain | Inconclusive candidate got Decision |
| AC6-03 | Inconclusive retained in Judgement evidence | judged_experiment (all inconclusive) | Verify Judgement record | `inconclusive` present in judgement evidence | — | Domain | Inconclusive lost from evidence |
| AC6-04 | Inconclusive retained in Proposal evidence | judged_experiment (all inconclusive) | Verify Proposal record | `inconclusive` present in proposal entries | — | Domain | Inconclusive lost from proposal |
| AC6-05 | No inconclusive in verdict projections | confirmed_experiment | Query `contextual_verdict_projections` | Only `win`/`learn`/`lose` in `verdict` column (CHECK constraint) | No `inconclusive` rows | Domain | Inconclusive in projection |
| AC6-06 | Mock execution_mode traceable through lineage | confirmed_experiment (mock mode) | Query verdict projections; join to ExperimentVersion → experiment_version_candidates | `execution_mode='mock'` on candidate record, traceable through immutable Version lineage | — | Domain | Mode lineage lost or broken |
| AC6-07 | Sandbox execution_mode traceable through lineage | confirmed_experiment (sandbox mode) | Query verdict projections; join to ExperimentVersion → experiment_version_candidates | `execution_mode='sandbox'` on candidate record, traceable through immutable Version lineage | — | Domain | Mode lineage lost or broken |
| AC6-08 | No Localization unlock on confirmation (win) | confirmed_experiment (win, byok mode) | Side-effect spy on Localization/Scale unlock interfaces | Zero calls to any unlock interface | No Localization/Scale side effect | Domain | Unlock side effect invoked |
| AC6-09 | No Scale unlock on confirmation (win) | confirmed_experiment (win, byok mode) | Side-effect spy on Localization/Scale unlock interfaces | Zero calls to any unlock interface | No Localization/Scale side effect | Domain | Unlock side effect invoked |
| AC6-10 | No unlock on mock confirmation | confirmed_experiment (mock mode) | Side-effect spy on unlock interfaces | Zero calls | No side effect | Domain | Mock triggered unlock |
| AC6-11 | No unlock on sandbox confirmation | confirmed_experiment (sandbox mode) | Side-effect spy on unlock interfaces | Zero calls | No side effect | Domain | Sandbox triggered unlock |

### AC-7: Replays never rewrite Judgements/Decisions or duplicate notifications

| Test ID | Scenario | Fixture | Command | Expected persisted | Expected absent | Layer | Failure diagnostic |
|---|---|---|---|---|---|---|---|
| AC7-01 | Same-Idempotency-Key judge retry | judged_experiment | `POST /api/experiments/{id}/judge` (same key) | 200 with original Judgement | No new Judgement, no new Proposal | API | Replay created duplicate |
| AC7-02 | Same-Idempotency-Key confirm retry | confirmed_experiment | `POST /api/experiments/{id}/proposals/{pid}/confirm` (same key) | 200 with original Confirmation | No new Decisions, no new projections | API | Replay created duplicate |
| AC7-03 | Same-key replay before current guards | confirmed_experiment (now closed) | `POST /api/experiments/{id}/proposals/{pid}/confirm` (same key) | 200 with original outcome | No re-authorization, no new side effects | API | Replay re-ran guards |
| AC7-04 | Identical judge evidence, new key, same eligibility context | running_experiment | Two `POST /api/experiments/{id}/judge` with different keys, same evidence, identical `eligibility_context_hash` | Exactly 1 Judgement (INSERT-or-load); exactly 1 Proposal (INSERT-or-load by uniqueness) | No duplicate Judgement, no duplicate Proposal | Domain | Duplicate Judgement or Proposal created |
| AC7-05 | Identical judge evidence, new key, changed eligibility context | running_experiment (eligibility changed between calls) | Two judge calls; eligibility context changed between | 1 Judgement; 2 Proposals (different `eligibility_context_hash`) | No duplicate Judgement | Domain | Changed context did not produce new Proposal |
| AC7-06 | Eligibility event redelivery | frozen_experiment | Replay same eligibility event | No-op; existing event returned | No duplicate event, no projection re-fold | Domain | Duplicate event appended |
| AC7-07 | Projection rebuild idempotent | frozen_experiment (with events) | Rebuild eligibility projection | head_hash matches pre-rebuild | No source event mutation | Domain | Rebuild changed source data |
| AC7-08 | Replay does not rewrite earlier Decision | confirmed_experiment | Re-invoke confirmation pipeline | Original Decisions unchanged | No Decision UPDATE or DELETE | Domain | Earlier Decision rewritten |
| AC7-09 | Replay does not rewrite earlier Judgement | judged_experiment | Re-invoke judge pipeline | Original Judgement unchanged | No Judgement UPDATE or DELETE | Domain | Earlier Judgement rewritten |
| AC7-10 | Notification deduplication (§A5) | confirmed_experiment | Same-key confirm retry | — | No duplicate outbox row | Domain | Duplicate outbox row created |

---

## §5.3 Unit and property test vectors

### §5.3.1 Canonical JSON encoder

| Test ID | Input | Expected | Layer | Failure diagnostic |
|---|---|---|---|---|
| CE-01 | `_canonical_json({"b": 1, "a": 2})` | `'{"a":2,"b":1}'` | Unit | Key sorting broken |
| CE-02 | `_canonical_json({"a": Decimal("1.230000")})` | `'{"a":"1.23"}'` | Unit | Trailing zeros not stripped |
| CE-03 | `_canonical_json({"a": Decimal("1000")})` | `'{"a":"1000"}'` | Unit | Exponent notation emitted (1E+3) |
| CE-04 | `_canonical_json({"a": Decimal("-0")})` | `'{"a":"0"}'` | Unit | Negative zero not collapsed |
| CE-05 | `_canonical_json({"a": Decimal("0.001")})` | `'{"a":"0.001"}'` | Unit | Small decimal mangled |
| CE-06 | `_canonical_json(1.5)` | `TypeError` | Unit | Binary float not rejected by `_reject_floats` |
| CE-07 | `_canonical_json({"a": [1.5]})` | `TypeError` | Unit | Nested float not rejected |
| CE-08 | `_canonical_json({1: "v"})` | `TypeError` | Unit | Non-string dict key not rejected |
| CE-09 | `_canonical_json({"a": "v"})` | `'{"a":"v"}'` | Unit | String key incorrectly rejected |
| CE-10 | `_canonical_json({"a": datetime(2024,1,1,12,0,0,123456,tzinfo=UTC)})` | `'{"a":"2024-01-01T12:00:00.123456Z"}'` | Unit | Microsecond precision lost |
| CE-11 | `_canonical_json({"a": datetime(2024,1,1,14,0,0,0,tzinfo=+02:00)})` | `'{"a":"2024-01-01T12:00:00.000000Z"}'` | Unit | Offset not normalized to UTC |
| CE-12 | `_canonical_json({"a": datetime(2024,1,1,12,0,0)})` | `ValueError` | Unit | Naive datetime not rejected |
| CE-13 | `_canonical_json({"a": (1, 2, 3)})` | `'{"a":[1,2,3]}'` | Unit | Tuple→list conversion broken |
| CE-14 | `_canonical_json({"a": Decimal("inf")})` | `ValueError` | Unit | Non-finite Decimal not rejected |
| CE-15 | `_canonical_json({"a": Decimal("NaN")})` | `ValueError` | Unit | NaN Decimal not rejected |
| CE-16 | `_canonical_json({"a": object()})` | `TypeError` | Unit | Unserializable type not rejected |

### §5.3.2 `_canonical_decimal_str` property tests

| Test ID | Description | Layer | Strategy |
|---|---|---|---|
| CD-P1 | For any finite Decimal `d`, `_canonical_decimal_str(d)` never contains `E` or `e` | Property | `hypothesis.strategies.decimals(allow_nan=False, allow_infinity=False)` |
| CD-P2 | For any finite Decimal `d`, `Decimal(_canonical_decimal_str(d)) == d` (round-trip) | Property | Same strategy |
| CD-P3 | `_canonical_decimal_str(d)` never starts with `-0` unless the result is `"0"` | Property | Same strategy |
| CD-P4 | If `d` has trailing fractional zeros, `_canonical_decimal_str(d)` has none | Property | Decimals with explicit trailing zeros |

### §5.3.3 `_normalize_legacy_numerics` tests

| Test ID | Input | Expected | Layer |
|---|---|---|---|
| NL-01 | `{"a": 1.5, "b": [2.0, {"c": 3.5}]}` | `{"a": Decimal("1.5"), "b": [Decimal("2.0"), {"c": Decimal("3.5")}]}` | Unit |
| NL-02 | `{"a": "text", "b": 42, "c": None}` | Unchanged | Unit |
| NL-03 | `{"a": 1e308}` | `Decimal` representation of `1e308` (no overflow) | Unit |

### §5.3.4 Hash and fingerprint determinism

| Test ID | Description | Layer | Failure diagnostic |
|---|---|---|---|
| HD-01 | `_sha256(_canonical_bytes(vector))` produces identical hash on PostgreSQL and SQLite for `canonical_test_vector` | Unit | Cross-dialect hash divergence |
| HD-02 | Truth-dependency hash for `(v1-bootstrap, False, None, "grandfathered_pre_truth_system")` is deterministic | Unit | Bootstrap hash not reproducible |
| HD-03 | Truth-dependency hash for `(v1-bootstrap, True, "tp-001", "grandfathered_pre_truth_system")` is deterministic | Unit | Bootstrap hash with truth_package not reproducible |
| HD-04 | QC evidence hash from `(rule_id, rule_version, check_type, severity, outcome, evidence_obj)` is deterministic | Unit | Evidence hash not reproducible |
| HD-05 | `eligibility_context_hash` from 4-input tuple is deterministic for fixed inputs | Unit | Context hash not reproducible |
| HD-06 | `proposal_hash` from `(eligibility_context_hash, judgement_hash, entries_canonical)` is deterministic | Unit | Proposal hash not reproducible |
| HD-07 | `context_hash` from `(experiment_id, offer_version_id, market, placement, lpe_hash)` is deterministic | Unit | Decision context hash not reproducible |
| HD-08 | `duplicate_group_hash` from `(dimension_key, concept_key_id)` is deterministic | Unit | Concept duplicate hash not reproducible |
| HD-09 | `candidate_hash` chain is deterministic for fixed candidate inputs | Unit | Candidate hash chain broken |

### §5.3.5 Eligibility-context hash sensitivity

| Test ID | Mutation | Expected | Layer | §4.11 check |
|---|---|---|---|---|
| ECH-01 | Change `frozen_snapshot_hash` (input 1) | Hash changes | Unit | 30 |
| ECH-02 | Change version-level projection head (input 2) | Hash changes | Unit | 31 |
| ECH-03 | Change recheck evidence hash (input 4) | Hash changes | Unit | 32 |
| ECH-04 | Add a candidate to the set (input 3) | Hash changes | Unit | 33 |
| ECH-05 | Remove a candidate from the set (input 3) | Hash changes | Unit | 33 |
| ECH-06 | Swap two candidates' head hashes (keep IDs) | Hash changes | Unit | 34 |
| ECH-07 | Change enumeration order of same `(id, hash)` set | Hash unchanged | Unit | 35 |

### §5.3.6 Immutable snapshot integrity

| Test ID | Description | Layer |
|---|---|---|
| SI-01 | Normalized integrity projection columns equal values extracted from `frozen_snapshot_json` via canonical decode | Unit |
| SI-02 | `frozen_snapshot_hash` equals `_sha256(_canonical_bytes(frozen_snapshot_json))` | Unit |
| SI-03 | `candidates_hash` equals `_sha256` of ordered candidate hashes | Unit |
| SI-04 | `concept_resolutions_hash` equals `_sha256` of resolution records | Unit |

---

## §5.4 Domain and service tests

### §5.4.1 Experiment lifecycle

| Test ID | Scenario | Precondition | Action | Expected | Layer |
|---|---|---|---|---|---|
| LC-01 | Create schema-2 experiment | Valid workspace, product | `create_experiment()` | Experiment with `record_schema_version=2`, revision=1 | Domain |
| LC-02 | Add entry | Draft experiment | `add_entry()` | Entry created, revision incremented | Domain |
| LC-03 | Update entry | Draft with entry | `update_entry()` | Entry updated, revision incremented | Domain |
| LC-04 | Remove entry | Draft with entry | `remove_entry()` | Entry removed, revision incremented | Domain |
| LC-05 | Update draft context | Draft experiment | `update_context()` | Context updated, revision incremented | Domain |
| LC-06 | Freeze experiment | Draft, all gates pass | `freeze_experiment()` | Version + Candidates + Concepts + Resolutions atomically; snapshot hash verified | Domain |
| LC-07 | Bind candidate | Frozen experiment | `bind_candidate()` | Binding created with version candidate FK | Domain |
| LC-08 | Start experiment | Frozen, bound, eligible | `start_experiment()` | State → running; `started_at` set | Domain |
| LC-09 | Run judgement | Running experiment | `run_judgement()` | Judgement + Proposal; proposal state = `created` | Domain |
| LC-10 | Confirm proposal | Valid proposal, eligible | `confirm_proposal()` | Confirmation + Decisions + verdict projections | Domain |
| LC-11 | Close (completed) | Running, ≥1 judgement | `close_experiment(completed)` | State → closed; closure evidence persisted | Domain |
| LC-12 | Close (abandoned_before_start) | Frozen only | `close_experiment(abandoned_before_start)` | State → closed | Domain |
| LC-13 | Close (aborted) | Frozen or running | `close_experiment(aborted)` | State → closed | Domain |
| LC-14 | Close (invalidated) | Active irreversible event | `close_experiment(invalidated)` | State → closed | Domain |
| LC-15 | Close (completed) requires judgement | Running, no judgements | `close_experiment(completed)` | 409 `no_judgements` | Domain |
| LC-16 | Close (invalidated) requires irreversible | Running, no irreversible event | `close_experiment(invalidated)` | 409 `no_active_invalidation` | Domain |
| LC-17 | Close (abandoned_before_start) from running | Running | `close_experiment(abandoned_before_start)` | 409 wrong state | Domain |

### §5.4.2 Freeze gates

| Test ID | Gate | Failing condition | Expected | Layer |
|---|---|---|---|---|
| FG-01 | Gate 1: effective review | Latest review ≠ `approved` | Fail-closed; freeze blocked | Domain |
| FG-02 | Gate 1: no review exists | No review rows | Fail-closed; freeze blocked | Domain |
| FG-03 | Gate 1: NULL review_value | Latest row has NULL value | Fail-closed; freeze blocked | Domain |
| FG-04 | Gate 1: total order tie-breaking | Two reviews at same `created_at`; latest by `id DESC` is `rejected` | Fail-closed; freeze blocked | Domain |
| FG-05 | Gate 1: legacy_unscoped exclusion | Only review has `legacy_unscoped=true` | Excluded from effective set; Gate 1 fails | Domain |
| FG-06 | Gate 1: wrong AssetVersion exclusion | Only review is for a different `asset_version_id` | Excluded; Gate 1 fails | Domain |
| FG-07 | Gate 2: QC unresolved blocking | Active `failed_blocking` attempt | Fail; freeze blocked | Domain |
| FG-08 | Gate 2: QC advisory (failed_nonblocking) | `failed_nonblocking` with no unresolved block | Warn; freeze proceeds | Domain |
| FG-09 | Gate 3: QC active attempt | Non-terminal attempt exists | Fail; freeze blocked | Domain |
| FG-10 | Gate 12: execution_mode uniformity | Mixed modes across entries | Fail; freeze blocked | Domain |
| FG-11 | Gate 13: truth-dependency NULL | NULL `truth_dependency_expected` | Fail-closed; freeze blocked | Domain |
| FG-12 | Gate 13: stale truth (evaluable but outdated) | `expected=true`; evidence evaluable but outdated | Fail; freeze blocked with `stale_truth` result | Domain |
| FG-12a | Gate 13: unevaluable truth (evidence/model absent) | `expected=true`; required dependency cannot be evaluated | Fail; freeze blocked with `stale_truth_unevaluable` result | Domain |
| FG-13 | Gate 13: truth expected=false | `expected=false` | Pass (`not_applicable`); no evaluator call | Domain |
| FG-14 | Gate 14: withdrawal | Withdrawn ProductVersion | Fail; freeze blocked | Domain |
| FG-15 | Gate 15: concept resolution incomplete | Unresolved duplicate group | Fail; freeze blocked | Domain |
| FG-16 | Gate 16: candidate count < 2 | One surviving candidate | `insufficient_candidates_after_resolution` (§2 Gate 16); HTTP status proposed in Addendum §A9 | Domain |
| FG-17 | Gate 16: candidate count > 100 | 101 candidates | `409 candidate_limit_exceeded` (§3.2) | Domain |
| FG-18 | Gate 16: candidate count 2–100 | Valid count | Pass; freeze proceeds | Domain |
| FG-19 | All gates pass | Valid experiment | Freeze succeeds | Domain |
| FG-20 | Advisory at draft: Gates 1,2,4,5 | Missing review/QC at draft creation | Experiment created; warnings returned | Domain |

### §5.4.3 Eligibility event and projection

| Test ID | Scenario | Expected | Layer | §4.11 checks |
|---|---|---|---|---|
| EL-01 | Append reversible event (`review_rejected`) | Projection updated; `current_eligibility=ineligible`; `irreversible_active=false` | Domain | — |
| EL-02 | Append restoration event (`review_re_approved`) | Projection restored; `current_eligibility=eligible` | Domain | — |
| EL-03 | Append irreversible event (`product_version_withdrawn`) | `irreversible_active=true`; all open proposals get terminal `invalidated` state event | Domain | — |
| EL-04 | Idempotent duplicate event (partial unique dedup) | No-op; existing row returned | Domain | — |
| EL-05 | Delayed-delivery monotonic `observed_at` | Incremental fold head_hash == rebuild head_hash | Domain | 36 |
| EL-06 | Equal wall-clock timestamps | Service advances by 1μs; rebuild order matches | Domain | 37 |
| EL-07 | In-place rebuild | head_hash, head_sequence, eligibility, irreversible all match pre-rebuild | Domain | 38 |
| EL-08 | Baseline head_hash | Non-NULL deterministic hash at projection creation | Domain | 39 |
| EL-09 | CAS on head_sequence | Stale sequence → rejection | Domain | 40 |
| EL-10 | Version-level + candidate-level composite eligibility | Both must be `eligible` for effective eligibility | Domain | — |
| EL-11 | Post-freeze QC pending/running blocks start | QC attempt in `pending` or `running` state | `start_experiment()` blocked | Domain | — |
| EL-12 | Post-freeze QC pending/running blocks confirm | QC attempt in `pending` or `running` state | `confirm_proposal()` blocked | Domain | — |

### §5.4.4 Proposal, confirmation, and decision pipeline

| Test ID | Scenario | Expected | Layer | §4.11 checks |
|---|---|---|---|---|
| DP-01 | Create proposal | Proposal + `created` state event; `eligibility_context_hash` matches formula | Domain | 41 |
| DP-02 | Proposal uniqueness | Same `(workspace, judgement, eligibility_context_hash)` → idempotent return | Domain | 41 |
| DP-03 | Confirm proposal | Confirmation + Decisions for non-inconclusive candidates + verdict projections | Domain | 42, 43 |
| DP-04 | Confirmation per-proposal unique | Second confirmation for same proposal → idempotent | Domain | 42 |
| DP-05 | Idempotency-key reuse | Same key, different proposal → `409 idempotency_key_reused` | Domain | 43 |
| DP-06 | Concurrent confirmation race (different keys) | Two sync psycopg connections with different `Idempotency-Key`, coordinated with `threading.Barrier`; exactly one 201 and one normalized `409`; raw unique violation must NOT escape to service/API boundary; fail on `40P01` (deadlock) or unexplained timeout | Integration-PG | 44 |
| DP-06a | Concurrent confirmation race (same key) | Two connections with same `Idempotency-Key`; exactly one write; loser gets observationally equivalent `200` replay | Integration-PG | 44 |
| DP-06b | Raw SQL unique constraint (DB-level) | Direct SQL INSERT of duplicate confirmation (bypassing service); unique violation at database level | Integration-PG | 44 |
| DP-07 | Confirm during active irreversible invalidation | Rejected (proposal terminally `invalidated`) | Domain | — |
| DP-08 | All-inconclusive blocks confirmation | No material verdicts → `409 no_material_verdicts` | Domain | — |
| DP-09 | Proposal state event sequence | Events respect monotonic `sequence` per proposal | Domain | 41 |
| DP-10 | Decision actor_id equals Confirmation.actor_id | Mismatch → service rejection | Domain | — |
| DP-11 | Decision confirmed_at equals Confirmation.confirmed_at | Mismatch → service rejection | Domain | — |
| DP-12 | Decision context_hash computation | Verified against `_sha256((exp_id, offer_ver_id, market, placement, lpe_hash))` | Domain | — |
| DP-13 | Confirmation rollback: Decision write failure | Inject failure → no Confirmation, no Decisions, no projections | Domain | — |
| DP-14 | Confirmation rollback: projection write failure | Inject failure → no Confirmation, no Decisions, no projections | Domain | — |
| DP-15 | Born-invalid proposal | Judge during active invalidation → Proposal born `created+invalidated` atomically; terminally non-confirmable | Domain | — |
| DP-16 | Irreversible invalidation idempotent propagation | Replay invalidation on already-invalidated proposal → no-op | Domain | — |

### §5.4.5 Invalidation and restoration

| Test ID | Scenario | Expected | Layer |
|---|---|---|---|
| IV-01 | Irreversible event: terminal proposal state event | All open proposals get `invalidated` state event; `irreversible_active=true` | Domain |
| IV-02 | Irreversible: confirm invalidated proposal | 409 rejection; proposal is terminal | Domain |
| IV-03 | Born-invalid proposal (judge during irreversible) | Proposal born `created+invalidated`; never confirmable | Domain |
| IV-04 | Reversible event: no terminal state event | Confirmation blocked; **no** Proposal state event appended; proposal stays pending | Domain |
| IV-05 | Reversible restoration: may confirm if fresh | After `review_re_approved`, confirmation proceeds **only if** proposal_hash valid, expected_proposal_sequence matches, proposal not superseded | Domain |
| IV-06 | Reversible restoration: stale proposal | After restoration, stale proposal_hash → 409 | Domain |
| IV-07 | `superseded` library version | Does NOT invalidate; no eligibility event | Domain |
| IV-08 | Close with `invalidated` | Requires active irreversible event | Domain |
| IV-09 | Close with `completed` | Requires ≥1 judgement | Domain |
| IV-10 | Irreversible propagation idempotent | Replay invalidation event → existing `invalidated` state event returned; no duplicate | Domain |

### §5.4.6 Optimistic concurrency

| Test ID | Scenario | Expected | Layer |
|---|---|---|---|
| OC-01 | Freeze with stale revision | `409 revision_conflict` | Domain |
| OC-02 | Context update with stale revision | `409 revision_conflict` | Domain |
| OC-03 | Confirm with stale proposal sequence | `409 sequence_conflict` | Domain |
| OC-04 | Entry add with stale revision | `409 revision_conflict` | Domain |
| OC-05 | Bind with stale revision | `409 revision_conflict` | Domain |
| OC-06 | Start with stale revision | `409 revision_conflict` | Domain |
| OC-07 | Judge with stale revision | `409 revision_conflict` | Domain |
| OC-08 | Close with stale revision | `409 revision_conflict` | Domain |

### §5.4.7 Schema-version dispatch

| Test ID | Scenario | Expected | Layer |
|---|---|---|---|
| SD-01 | Create with legacy shape post-migration | `422 legacy_create_shape_retired` | Domain |
| SD-02 | Legacy experiment uses legacy read path (shared URL) | Returns legacy fields | Domain |
| SD-03 | Schema-2 experiment uses schema-2 read path (shared URL) | Returns schema-2 fields | Domain |
| SD-04 | Schema-2 exclusive URL with legacy experiment | `409 schema_version_mismatch` | Domain |
| SD-05 | Legacy exclusive URL with schema-2 experiment | `409 schema_version_mismatch` | Domain |
| SD-06 | `set_verdict()` on any experiment | `410 verdict_endpoint_retired` | Domain |
| SD-07 | Legacy and schema-2 coexist in same workspace | Both accessible; no interference | Domain |

### §5.4.8 Lock order verification

Lock call-ordering (class sequence correctness) is tested at Domain layer via instrumented lock acquisition. Deadlock freedom under real row locks is tested at Integration-PG (§5.6.9). Class 7 (`ExperimentTimelineHead`) is part of the complete design (§A2).

| Test ID | Scenario | Expected | Layer |
|---|---|---|---|
| LO-01 | Freeze acquires locks in class order 1→2→3→4→7 | Correct order verified by instrumentation | Domain |
| LO-02 | Start acquires locks in class order 1→2→3→4→5→7 | Correct order | Domain |
| LO-03 | Judge acquires locks in class order 1→2→3→4→5→6→7 | Correct order | Domain |
| LO-04 | Confirm acquires locks in class order 1→2→3→4→5→6→7 | Correct order | Domain |
| LO-05 | Timeline sequence allocation locks class 7 last | Class 7 after all prior classes | Domain |

### §5.4.9 Immutable snapshot negatives

| Test ID | Scenario | Expected | Layer |
|---|---|---|---|
| IS-01 | Service: update frozen_snapshot_json | Rejected; no mutation | Domain |
| IS-02 | Service: update candidate hash after freeze | Rejected; no mutation | Domain |
| IS-03 | Service: update concept resolution after freeze | Rejected; no mutation | Domain |
| IS-04 | Service: delete ExperimentVersion | Rejected; no deletion | Domain |
| IS-05 | Service: delete Candidate after freeze | Rejected; no deletion | Domain |

---

## §5.5 Effective-review, QC, truth, conflict, and invalidation matrices

### §5.5.1 Review coverage

| Test ID | Scenario | Expected | Layer |
|---|---|---|---|
| RC-01 | No review exists | Fail-closed: NULL/unrecognized → ineligible | Domain |
| RC-02 | Latest review = `approved` | Eligible | Domain |
| RC-03 | Latest review = `rejected` | Ineligible; eligibility event emitted | Domain |
| RC-04 | Later rejection supersedes approval | Ineligible (total order `(created_at DESC, id DESC)`) | Domain |
| RC-05 | Later re-approval after rejection | Re-eligible; restoration event emitted | Domain |
| RC-06 | NULL `review_value` | Fail-closed: treated as rejection | Domain |
| RC-07 | Total order tie: same `created_at`, latest `id DESC` wins | Correct ordering | Domain |
| RC-08 | `legacy_unscoped=true` review excluded | Not in effective set | Domain |
| RC-09 | Review for wrong `asset_version_id` excluded | Not in effective set | Domain |

### §5.5.2 QC coverage and remediation

| Test ID | Scenario | Expected | Layer | §4.11 checks |
|---|---|---|---|---|
| QC-01 | QC not expected + no attempts | Pass; QC gate is not a barrier | Domain | — |
| QC-02 | QC expected + no attempts | Warn; human review remains mandatory; advisory non-blocking | Domain | — |
| QC-03 | All relevant blocking attempts cleared | Every `failed_blocking` attempt is either nonblocking under the approved policy or has a valid linked remediation with matching `input_hash` and `covers=True`; a later `passed` attempt alone does NOT clear an earlier `failed_blocking` → QC-cleared | Domain | — |
| QC-04 | Latest terminal attempt = `failed_blocking` | Not QC-cleared; eligibility event | Domain | — |
| QC-05 | Active (non-terminal) attempt exists: `qc_in_progress` | Partial unique blocks second active; start/confirm blocked | Domain | 88 |
| QC-06 | Valid remediation: same AV, `failed_blocking` parent, lower sequence, `covers=True` | Specifically linked unresolved block cleared | Domain | 87 |
| QC-07 | Remediation: unrelated block keeps AV blocked | Other unresolved `failed_blocking` not cleared by this remediation | Domain | 87 |
| QC-08 | Remediation cycle detection | Rejected | Domain | 87 |
| QC-09 | Remediation wrong AV | Rejected (different `asset_version_id`) | Domain | 87 |
| QC-10 | Remediation: nonexistent `remediates_attempt_id` | Rejected by FK/service validation | Domain | 87 |
| QC-11 | Remediation: parent already remediated | Second child at already-remediated block does not clear; block-resolution projection deterministic | Domain | 87 |
| QC-12 | Remediation cap exceeded | `max_qc_attempts_per_asset_version` → rejected under AV lock | Domain | 87 |
| QC-13 | `attempt_sequence` uniqueness | `UNIQUE(workspace_id, asset_version_id, attempt_sequence)` enforced | Domain | 86 |
| QC-14 | `attempt_sequence` write-once | UPDATE after creation → rejected | Integration-PG | 61 |
| QC-15 | `remediates_attempt_id` write-once | UPDATE after creation → rejected | Integration-PG | 61 |
| QC-16 | Remediated QC → `qc_remediated` eligibility event | Restoration event for candidate | Domain | — |
| QC-17 | `covers` reflexive equality | Same `(policy_version, rule_set_version)` → `covers=True` with `registry_version` and `proof_hash` | Domain | — |
| QC-18 | `covers` transitive (registered supersession) | Newer version registered as covering older → `covers=True` | Domain | — |
| QC-19 | `covers` unknown/unrelated pair | Unregistered pair → `covers=False` (fail-closed) with `registry_version` and `proof_hash` | Domain | — |
| QC-20 | Remediation `input_hash` mismatch | Passing remediation attempt's `input_hash` ≠ blocked attempt's `input_hash` → invalid remediation; block remains unresolved | Domain | — |
| QC-21 | Earliest unresolved block | Multiple `failed_blocking` attempts; service returns the earliest by sequence | Domain | — |
| QC-22 | Active attempt partial unique (PG) | Two active attempts for same AV | Unique violation | Integration-PG | 88 |
| QC-23 | Bounded evidence: count + set hash, no ID list | `unresolved_block_count` + `unresolved_block_set_hash`; causal links only | Domain | — |

### §5.5.3 Truth-dependency

| Test ID | Scenario | Expected | Layer | §4.11 checks |
|---|---|---|---|---|
| TD-01 | NULL `truth_dependency_expected` at freeze | Gate 13 fail-closed rejection | Domain | 82 |
| TD-02 | `expected=true`, valid truth | Gate 13 pass | Domain | — |
| TD-03 | `expected=true`, stale truth | Gate 13 rejection + `stale_truth_detected` event | Domain | — |
| TD-04 | `expected=false`, NULL `truth_package_id` | Gate 13 pass (`not_applicable`); no evaluator call | Domain | — |
| TD-05 | `expected=false`, non-NULL `truth_package_id` | CHECK violation | Integration-PG | 83 |
| TD-06 | Non-NULL `truth_package_id`, `expected != true` | CHECK violation | Integration-PG | 83 |
| TD-07 | Truth write-once: change after set | Trigger rejection | Integration-PG | 84 |
| TD-08 | `v1-bootstrap` on post-cutover INSERT | Trigger rejection | Integration-PG | 85 |
| TD-09 | `v1-bootstrap` on post-cutover UPDATE init | Trigger rejection | Integration-PG | 85 |
| TD-10 | Atomic four-field initialization | All four set together → allowed; partial → rejected | Integration-PG | 84 |
| TD-11 | Truth unevaluable blocks freeze | `stale_truth_unevaluable` returned as freeze result; freeze blocked; no Version created | Domain | — |
| TD-12 | Persisted evidence/hash mismatch | Evidence hash ≠ `_sha256(canonical(evidence))` → service rejection | Domain | — |
| TD-13 | No runtime synthesis of grandfathering | `v1-bootstrap` is migration-only; service code path cannot produce it | Unit (source scan) | — |

### §5.5.4 Candidate-conflict and concept resolution

| Test ID | Scenario | Expected | Layer |
|---|---|---|---|
| CC-01 | Two candidates share `(dimension_key, concept_key_id)` with no resolution | `duplicate_group_hash` computed; freeze gate 15 rejects | Domain |
| CC-02 | `allow_controlled_duplicate` resolution | Both candidates survive; resolution record created | Domain |
| CC-03 | `exclude_source_candidate` resolution | Source candidate excluded; surviving count checked | Domain |
| CC-04 | `remap_before_freeze` resolution | Concept remapped; new `duplicate_group_hash` | Domain |
| CC-05 | Resolution plan references nonexistent entry | 422 rejection | Domain |
| CC-06 | Resolution plan incomplete (unresolved group) | Gate 15 rejects | Domain |
| CC-07 | `duplicate_group_hash` NOT NULL on every concept row | Verified post-freeze | Domain |
| CC-08 | Resolution authorization | Actor lacks `experiment.freeze` permission → rejected | Domain |
| CC-09 | Complete group membership | Resolution must cover all members of the duplicate group | Domain |
| CC-10 | Stale revision in resolution plan | `409 revision_conflict` | Domain |
| CC-11 | Stale source fingerprint in resolution | Source candidate changed since plan → rejected | Domain |
| CC-12 | Deterministic duplicate_group_hash | `_sha256((dimension_key, concept_key_id))` is reproducible | Unit |
| CC-13 | Surviving count after exclude reduces below 2 | `insufficient_candidates_after_resolution` (§2 Gate 16); HTTP status per Addendum §A9 | Domain |

### §5.5.5 Post-freeze validity

| Test ID | Scenario | Expected | Layer |
|---|---|---|---|
| PFV-01 | Post-freeze review rejection | Eligibility event emitted; does NOT unfreeze | Domain |
| PFV-02 | Post-freeze QC failure | Eligibility event emitted; does NOT unfreeze | Domain |
| PFV-03 | Post-freeze truth staleness | Eligibility event emitted; does NOT unfreeze | Domain |
| PFV-04 | `superseded` library version post-freeze | Allowed; no eligibility event | Domain |
| PFV-05 | Post-freeze validity distinct from freeze eligibility | Superseded allowed post-freeze, not at freeze | Domain |

### §5.5.6 Bounded evidence

| Test ID | Scenario | Expected | Layer |
|---|---|---|---|
| BE-01 | Review evidence: count + set hash, no unbounded ID list | `review_set_hash` + `superseded_count`; no `superseded_review_ids` list | Domain |
| BE-02 | QC evidence: count + set hash, no unbounded ID list | `unresolved_block_count` + `unresolved_block_set_hash`; causal remediation links only | Domain |
| BE-03 | QC attempt cap enforced | Attempt creation rejected at `max_qc_attempts_per_asset_version` | Domain |
| BE-04 | Freeze gate evidence: canonical hashes per candidate | Per-candidate evidence includes canonical hashes and counts only | Domain |
| BE-05 | No unbounded history in frozen snapshot | `frozen_snapshot_json` contains canonical set hashes, not full historical rows | Domain |

---

## §5.6 PostgreSQL integration evidence

All tests in this section run on a real PostgreSQL 16 instance. They fail (not skip) when the PostgreSQL test URL or role fixtures are absent.

### §5.6.1 Composite FK enforcement

| Test ID | FK | Violation | Expected | §4.11 check |
|---|---|---|---|---|
| FK-01 | Cross-workspace FK insert | Child `workspace_id` ≠ parent's | FK violation | 8 |
| FK-02 | Eligibility event: mismatched experiment/version | 3-col FK to `experiment_versions` | FK violation | 10 |
| FK-03 | Proposal: Judgement from different experiment | 4-col FK `(w, exp, ev, jid)→judgements` | FK violation | 45 |
| FK-04 | Proposal: Judgement from different version | Same 4-col FK | FK violation | 46 |
| FK-05 | Proposal: Version from different experiment | 3-col FK `(w, exp, ev)→experiment_versions` | FK violation | 47 |
| FK-06 | Decision: candidate from different version | 3-col FK `(w, ev, cand)→candidates` | FK violation | 48 |
| FK-07 | Decision: confirmation from different proposal | 3-col FK `(w, conf, prop)→confirmations` | FK violation | 49 |
| FK-08 | Decision: experiment_id ≠ Proposal's | 5-col FK `(w, prop, exp, ev, jid)→proposals` | FK violation | 50 |
| FK-09 | Decision: experiment_version_id ≠ Proposal's | Same 5-col FK | FK violation | 51 |
| FK-10 | Decision: judgement_id ≠ Proposal's | Same 5-col FK | FK violation | 52 |

### §5.6.2 Partial uniques and CHECKs

| Test ID | Constraint | Test | Expected | §4.11 check |
|---|---|---|---|---|
| PU-01 | Ad bindings legacy CHECK | Legacy row missing `experiment_entry_id` | CHECK violation | 11 |
| PU-02 | Ad bindings schema-2 CHECK | Schema-2 row missing `experiment_version_candidate_id` | CHECK violation | 11 |
| PU-03 | Judgements bidirectional CHECK | Schema-2 with NULL `experiment_version_id` | CHECK violation | 12 |
| PU-04 | Decisions full CASE CHECK | Schema-2 with NULL `confirmation_id` | CHECK violation | 13 |
| PU-05 | Decisions full CASE CHECK | Legacy with non-NULL `proposal_id` | CHECK violation | 13 |
| PU-06 | `record_schema_version` CHECK | Value = 3 | CHECK violation | 14 |
| PU-07 | ProductProfile partial unique | Two products with same `product_profile_entry_id` in workspace | Unique violation | 19 |
| PU-08 | Active QC attempt partial unique | Two active attempts for same AV | Unique violation | 88 |

### §5.6.3 Append-only trigger enforcement

Append-only trigger tests use a DML-capable role (migration-test role or `marketai_erasure_principal` outside a valid erasure context) so the test actually reaches the trigger. API/worker roles lack UPDATE/DELETE grants on immutable tables and would fail at privilege checking before the trigger fires (tested separately in SN-02 and RG-03/RG-04).

The table count is dynamically derived from the `APPEND_ONLY_TABLES` registry; it is not hard-coded. If addendum tables are approved (§A10), the count increases accordingly and all new append-only tables join the test matrix.

| Test ID | Table | Operation | Expected | §4.11 checks |
|---|---|---|---|---|
| AO-01..N | Each table in `APPEND_ONLY_TABLES` (dynamically enumerated) | `UPDATE` via DML-capable role | Trigger rejection | 57 |
| AO-(N+1)..2N | Each table in `APPEND_ONLY_TABLES` (dynamically enumerated) | `DELETE` (non-erasure) via DML-capable role | Trigger rejection | 58 |
| AO-M1 | `experiment_eligibility_projections` | `UPDATE` | Allowed | 59 |
| AO-M2 | `experiment_eligibility_projections` | `DELETE` (non-erasure) | Trigger rejection | 59 |

### §5.6.4 Column write-once trigger enforcement

| Test ID | Column | Mutation | Expected | §4.11 check |
|---|---|---|---|---|
| WO-01 | `products.product_profile_entry_id` | NULL→value | Allowed | 21 |
| WO-02 | `products.product_profile_entry_id` | value→different | Rejected | 21 |
| WO-03 | `products.product_profile_entry_id` | value→NULL | Rejected | 21 |
| WO-04 | `experiments.record_schema_version` | UPDATE NULL→2 | Rejected | 15 |
| WO-05 | `experiments.record_schema_version` | UPDATE 2→NULL | Rejected | 15 |
| WO-06 | `experiments.record_schema_version` | UPDATE 2→2 | Rejected | 15 |
| WO-07 | `experiments.started_at` | NULL→value | Allowed | 16 |
| WO-08 | `experiments.started_at` | value→different | Rejected | 16 |
| WO-09 | `experiment_ad_bindings.record_schema_version` | UPDATE NULL→2 | Rejected | 17 |
| WO-10 | `experiment_ad_bindings.record_schema_version` | UPDATE 2→NULL | Rejected | 17 |
| WO-11 | `asset_qc_attempts.attempt_sequence` | UPDATE after set | Rejected | 61 |
| WO-12 | `asset_qc_attempts.remediates_attempt_id` | UPDATE after set | Rejected | 61 |

### §5.6.5 RLS, FORCE RLS, and signed tenant context

All RLS tests use the signed 3-GUC tenant context: `marketai.workspace_id`, `marketai.authorization_epoch`, `marketai.context_signature`. The SECURITY DEFINER function `marketai_context_is_current(candidate_workspace text)` (owned by the migration/admin role, validated by HMAC-SHA256) is the policy predicate. Runtime roles (API, worker, readonly) have EXECUTE but do not own the function and cannot read the signing secret.

| Test ID | Table | Assertion | Expected | §4.11 checks |
|---|---|---|---|---|
| RL-01 | Every new workspace-owned table | `ENABLE ROW LEVEL SECURITY` set | `pg_class.relrowsecurity = true` | — |
| RL-02 | Every new workspace-owned table | `FORCE ROW LEVEL SECURITY` set | `pg_class.relforcerowsecurity = true` | — |
| RL-03 | Every new table | `USING (marketai_context_is_current(workspace_id))` policy exists | Policy in `pg_policies` | — |
| RL-04 | Every new table | `WITH CHECK (marketai_context_is_current(workspace_id))` policy exists | Policy in `pg_policies` | — |
| RL-05 | API role cross-workspace SELECT | Different workspace_id in signed context | Empty result set | — |
| RL-06 | API role cross-workspace INSERT | Different workspace_id in signed context | RLS policy violation | — |
| RL-07 | Signed context: valid 3-GUC set | All three GUCs correct | `marketai_context_is_current(workspace_id)` returns `true` | — |
| RL-08 | Signed context: missing `marketai.workspace_id` | GUC not set | `marketai_context_is_current()` returns `false`; SELECT returns empty | — |
| RL-09 | Signed context: missing `marketai.authorization_epoch` | Epoch GUC not set | Returns `false` | — |
| RL-10 | Signed context: missing `marketai.context_signature` | Signature GUC not set | Returns `false` | — |
| RL-11 | Signed context: stale `authorization_epoch` | Epoch does not match current | Returns `false`; all RLS-protected operations fail | — |
| RL-12 | Signed context: bad `context_signature` | HMAC mismatch | Returns `false`; all operations fail | — |
| RL-13 | Signed context: cross-workspace | Valid signature for ws-test-002 but querying ws-test-001 data | Empty result set for ws-test-001 data | — |
| RL-14 | FORCE RLS: table owner SELECT | Owner role with FORCE RLS but no BYPASSRLS | RLS policy applies; cross-workspace SELECT returns empty | — |
| RL-15 | FORCE RLS: table owner INSERT | Owner role with FORCE RLS but no BYPASSRLS | RLS policy applies; cross-workspace INSERT rejected | — |

### §5.6.6 Role and grant matrix

Grants verified against the approved §4.9 role/grant matrix and addendum §A10.3. Existing mutable-table grants are unchanged; only Issue #22 new tables are tested. FORCE RLS subjects ordinary owners. `marketai_erasure_principal` is the narrowly authorized caller with EXECUTE on the erasure procedure only; it has zero direct table DML grants and no BYPASSRLS. A separate designated admin owner of the SECURITY DEFINER erasure procedure has BYPASSRLS and is not a runtime member. Only `marketai_domain_api` has UPDATE on `experiment_timeline_heads`; all other runtime roles (including Worker) have no UPDATE on any addendum table.

| Test ID | Role | Table class | Operation | Expected |
|---|---|---|---|---|
| RG-01 | `marketai_domain_api` | Immutable evidence tables | SELECT | Allowed |
| RG-02 | `marketai_domain_api` | Immutable evidence tables | INSERT | Allowed |
| RG-03 | `marketai_domain_api` | Immutable evidence tables | UPDATE | Denied |
| RG-04 | `marketai_domain_api` | Immutable evidence tables | DELETE | Denied |
| RG-05 | `marketai_domain_api` | Existing mutable tables | SELECT, INSERT, UPDATE | Per existing grants (unchanged by Issue #22) |
| RG-06 | `marketai_domain_api` | Verdict projections | SELECT, INSERT | Allowed |
| RG-07 | `marketai_domain_api` | Verdict projections | DELETE | Denied (only via SECURITY DEFINER function) |
| RG-08 | `marketai_domain_worker` | Same as API for new tables | Same grants/denials | Matches API |
| RG-09 | `marketai_projection_writer` | Immutable evidence tables | SELECT | Allowed |
| RG-10 | `marketai_projection_writer` | Immutable evidence tables | INSERT, UPDATE, DELETE | Denied |
| RG-11 | `marketai_projection_writer` | Eligibility projections | SELECT, UPDATE | Allowed |
| RG-12 | `marketai_projection_writer` | Eligibility projections | INSERT, DELETE | Denied (INSERT not granted; DELETE trigger) |
| RG-13 | `marketai_projection_writer` | Verdict projections | SELECT, INSERT, UPDATE, DELETE | Allowed |
| RG-14 | `marketai_projection_writer` | Existing tables | SELECT | Allowed |
| RG-15 | `marketai_erasure_principal` (caller) | Erasure procedure | EXECUTE | Allowed; caller invokes SECURITY DEFINER procedure |
| RG-15a | `marketai_erasure_principal` (caller) | All tables directly | UPDATE, DELETE | Denied (no BYPASSRLS, no direct DML grants on immutable tables); only operates through procedure |
| RG-15b | `marketai_erasure_principal` (caller) | — | BYPASSRLS check | `rolbypassrls = false`; no unintended role memberships |
| RG-16 | `marketai_domain_readonly` | All tables | SELECT | Allowed |
| RG-17 | `marketai_domain_readonly` | All tables | INSERT, UPDATE, DELETE | Denied |
| RG-18 | API/worker cannot `SET ROLE marketai_projection_writer` | `SET ROLE` | Permission denied | — |
| RG-19 | FORCE RLS: ordinary owners subjected | Owner role without BYPASSRLS | RLS policy applies to owner's operations | — |
| RG-20 | Erasure procedure OWNER: BYPASSRLS verified | Check `rolbypassrls` on the erasure procedure's OWNER role (designated admin, NOT `marketai_erasure_principal`) | `true` | — |
| RG-20a | Erasure CALLER: no BYPASSRLS | Check `rolbypassrls` on `marketai_erasure_principal` (the caller) | `false` | — |
| RG-21 | EXECUTE on `marketai_context_is_current` | API, worker, readonly roles | EXECUTE allowed | — |
| RG-22 | EXECUTE on `marketai_context_is_current` | Anonymous/PUBLIC | EXECUTE denied | — |

### §5.6.7 SECURITY DEFINER functions

| Test ID | Function | Test | Expected | §4.11 checks |
|---|---|---|---|---|
| SF-01 | `marketai_maintain_verdict_projection()` | Exists with correct owner | `marketai_projection_writer` | 71 |
| SF-02 | Same function | SECURITY DEFINER set | True | 71 |
| SF-03 | Same function | `search_path` locked to `''` | True | 71 |
| SF-04 | Same function | All internal references schema-qualified | Source inspection | 71 |
| SF-05 | Same function | PUBLIC execute revoked | True | 71 |
| SF-06 | Same function | EXECUTE granted to API + worker only | True | 71 |
| SF-07 | Same function | API role invokes: obsolete row deleted, replacement inserted atomically | Success | 62, 65 |
| SF-08 | Same function | Invalid tenant context (bad signature) | Rejected | 64 |
| SF-09 | Same function | No obsolete row (no-op) | Success (no error) | 62 |
| SF-10 | Same function | API role direct DELETE on verdict projections | Denied (no grant) | 63 |
| SF-11 | Same function | API role direct SQL equivalent of function body | DELETE denied, INSERT allowed | 63 |
| SF-12 | `marketai_context_is_current()` | SECURITY DEFINER, STABLE, owner = migration/admin role (NOT `marketai_domain_api`) | True | — |
| SF-13 | `marketai_context_is_current()` | PUBLIC execute revoked; EXECUTE to API, worker, readonly; runtime roles cannot read signing secret | Verified | — |
| SF-13a | `marketai_context_is_current()` | `search_path` locked to `''`; all internal references schema-qualified | True | — |
| SF-13b | `marketai_context_is_current()` | Fail-closed: missing/invalid signature → false; stale epoch → false | Verified | — |
| SF-14 | Erasure procedure | Exists; owner is designated admin role with BYPASSRLS (NOT `marketai_erasure_principal`) | True | 80 |
| SF-14a | Erasure procedure | SECURITY DEFINER, locked search_path, schema-qualified refs, PUBLIC revoked | True | — |
| SF-14b | Erasure procedure | EXECUTE granted to `marketai_erasure_principal` only; caller has no BYPASSRLS, no unintended role memberships | Verified | — |
| SF-14c | Erasure procedure | Erasure-context validation: verifies GUC + `pg_has_role` before proceeding | Verified | — |
| SF-15 | `marketai_projection_writer` grants | SELECT on immutable, SELECT+UPDATE on eligibility, full DML on verdict, SELECT on existing | Verified against §4.9 matrix | 71 |

### §5.6.8 Controlled erasure

| Test ID | Scenario | Expected | §4.11 checks |
|---|---|---|---|
| ER-01 | Full workspace erasure | All experiment-domain rows deleted in dependency order | 74 |
| ER-02 | Cross-workspace isolation | Other workspace rows untouched | 75 |
| ER-03 | Non-erasure DELETE on append-only | Rejected | 76 |
| ER-04 | Misordered deletion | RESTRICT FK blocks | 77 |
| ER-05 | API/worker sets erasure GUC + DELETE | `pg_has_role` rejects; record SQLSTATE | 78 |
| ER-06 | Audit receipt emission | Receipt reaches external sink, survives data erasure | 79 |
| ER-07 | Erasure procedure OWNER (designated admin) BYPASSRLS | `pg_has_role` + `rolbypassrls` on procedure OWNER (NOT caller `marketai_erasure_principal`) verified | 80 |
| ER-07a | Erasure CALLER no BYPASSRLS | `rolbypassrls = false` on `marketai_erasure_principal`; no unintended role memberships | — |

### §5.6.9 Concurrency and deadlock (real PostgreSQL)

All concurrency tests use **sync psycopg** connections coordinated with `threading.Barrier` — no `asyncio.Event`, no `sleep`-based timing. Primary tests use the production isolation level (`READ COMMITTED`). Each test specifies `SET lock_timeout = '5s'` on both connections. SQLSTATE and lock wait diagnostics are recorded on failure. A deadlock (`40P01`) or unexplained timeout is a test failure, not a pass. Automatic retries are not hidden; serialization failures are surfaced with diagnostics. A timeout alone is never treated as proof of correctness.

| Test ID | Scenario | Expected | Isolation |
|---|---|---|---|
| DC-01 | Two concurrent freezes on different experiments | Both succeed; no deadlock | Production (`READ COMMITTED`) |
| DC-02 | Concurrent freeze + start on same experiment | Serialized at class-1 lock; one succeeds, other gets 409 | Production |
| DC-03 | Concurrent eligibility event + confirm | Serialized at class-5; no deadlock | Production |
| DC-04 | Two concurrent confirms, different keys | Exactly one 201, one normalized 409; raw unique violation must NOT escape; no deadlock (`40P01` = failure) | Production |
| DC-04a | Two concurrent confirms, same key | Exactly one write; loser gets 200 replay | Production |
| DC-04b | Raw SQL unique constraint proof | Direct SQL duplicate INSERT → unique violation (separate DB-level test, not API/service boundary) | Production |
| DC-05 | Concurrent judge + close | Serialized at class-1; no deadlock | Production |
| DC-06 | Lock wait exceeds `lock_timeout` | Failure with SQLSTATE 55P03 recorded; test fails with diagnostics | Production |

A separate, optional stress suite exercises SERIALIZABLE isolation:

| Test ID | Scenario | Expected | Isolation |
|---|---|---|---|
| DC-S1 | Concurrent confirms under SERIALIZABLE | Serialization failure surfaced; SQLSTATE 40001 recorded | `SERIALIZABLE` |
| DC-S2 | Concurrent freeze + judge under SERIALIZABLE | Serialization failure surfaced | `SERIALIZABLE` |

The stress suite is scheduled/release tier; it does not block PR merge.

### §5.6.10 PostgreSQL trigger catalog

| Test ID | Assertion | §4.11 check |
|---|---|---|
| TC-01 | `marketai_reject_update_or_delete` function exists in `pg_proc` | 66 |
| TC-02 | Every trigger in `APPEND_ONLY_TABLES` exists in `pg_trigger` qualified by `tgrelid` | 67 |
| TC-03 | Every column write-once trigger exists in `pg_trigger` | 68 |
| TC-04 | No `CREATE TRIGGER IF NOT EXISTS` in migration source | 69 |
| TC-05 | #21's per-table function/trigger for `asset_qc_check_results` no longer exists; replaced by shared function | 70 |

---

## §5.7 SQLite parity tests

### §5.7.1 Supported on SQLite

All domain/service tests (§5.4) run on both SQLite and PostgreSQL. Canonical hash determinism (§5.3) is cross-dialect by design.

| Test ID | Description | §4.11 checks |
|---|---|---|
| SQ-01 | JSON round-trip: insert structured object via `SA_JSON`, read back, assert structural equality | 72 |
| SQ-02 | QC evidence hash: read via `SA_JSON` on SQLite, normalize, hash matches PostgreSQL | 73 |
| SQ-03 | Migration upgrade succeeds on SQLite | 2 |
| SQ-04 | Migration downgrade succeeds on SQLite (empty schema-2) | 3 |
| SQ-05 | Hash equality: `canonical_test_vector` produces identical hash on both dialects | 22 |

### §5.7.2 PostgreSQL-only guarantees

The following are NOT tested on SQLite. CI proves them on real PostgreSQL in the `domain-rls` job:

- Append-only triggers (§5.6.3)
- Column write-once triggers (§5.6.4)
- RLS, FORCE RLS, signed context, and policies (§5.6.5)
- SECURITY DEFINER functions (§5.6.7)
- Controlled erasure via trigger bypass (§5.6.8)
- `pg_trigger` catalog assertions (§5.6.10)
- Deadlock/concurrency with real row locks (§5.6.9)
- Role/grant enforcement (§5.6.6)
- Composite FK enforcement at database level (§5.6.1) — service-level equivalents run on SQLite

### §5.7.3 SQLite service guards

Where PostgreSQL uses triggers, SQLite relies on service-level guards tested via domain tests. The service guards are defense-in-depth; PostgreSQL triggers are authoritative. SQLite tests verify:

- Append-only service rejection of UPDATE/DELETE on immutable records
- Write-once service rejection of column mutation
- Schema-version discrimination via CASE CHECK equivalent
- Erasure service path with `is_erasure_principal` flag and dependency ordering

---

## §5.8 Migration and backfill verification

### §5.8.1 Upgrade lifecycle

| Test ID | Scenario | Expected | §4.11 checks |
|---|---|---|---|
| MU-01 | Clean upgrade PostgreSQL | All 8 steps complete; post-migration checks pass | 1 |
| MU-02 | Clean upgrade SQLite | All steps complete | 2 |
| MU-03 | Upgrade with legacy data | Backfills populate; NOT NULL enforced; no orphans | 7, 9 |
| MU-04 | Workspace-id backfill correctness | All child `workspace_id` matches parent experiment | 7 |
| MU-05 | Truth-dependency backfill | NULL truth_package → `expected=false`; non-NULL → `expected=true` | 81 |
| MU-06 | Truth hash reproducibility | Hash from `(v1-bootstrap, expected, truth_package_id, reason)` matches stored | 23 |
| MU-07 | QC sequence backfill | `attempt_sequence` = `ROW_NUMBER() OVER (PARTITION BY workspace_id, asset_version_id ORDER BY created_at, id)` | — |
| MU-08 | QC evidence hash backfill | Hash from `(rule_id, rule_version, check_type, severity, outcome, evidence_obj)` matches stored | 24 |
| MU-09 | QC evidence hash cross-dialect | SQLite hash matches PostgreSQL for same data | 73 |
| MU-10 | `frozen_snapshot_hash` UNIQUE→INDEX | Old UNIQUE dropped; INDEX created; no data loss | — |

### §5.8.2 Downgrade lifecycle

| Test ID | Scenario | Expected | §4.11 checks |
|---|---|---|---|
| MD-01 | Empty schema-2 downgrade PostgreSQL | Succeeds; all new tables/columns removed | 3 |
| MD-02 | Empty schema-2 downgrade SQLite | Succeeds | 3 |
| MD-03 | Schema-2 experiment exists | Abort naming `record_schema_version=2` | 4 |
| MD-04 | Evidence rows exist | Abort naming the table | 4 |
| MD-05 | Non-NULL ProductProfile link | Abort | 4 |
| MD-06 | Non-bootstrap truth determination | Abort | 4 |
| MD-07 | Non-NULL QC remediation link | Abort | 4 |
| MD-08 | Duplicate `frozen_snapshot_hash` | Abort before restoring UNIQUE | 5 |
| MD-09 | Duplicate `(ad_account_id, ad_id)` | Abort before restoring UNIQUE | 5 |
| MD-10 | #21 trigger restoration | Per-table function + trigger restored after downgrade | 6 |
| MD-11 | Data conservation | Backfilled workspace_id, bootstrap truth, QC hashes/sequence/terminal safely dropped; data conservation row counts and checksums recorded | — |

### §5.8.3 Preflight and postflight checks

| Test ID | Scenario | Expected |
|---|---|---|
| MC-01 | Orphan experiment_entries (no parent experiment) | Preflight aborts |
| MC-02 | Orphan experiment_ad_bindings | Preflight aborts |
| MC-03 | Orphan experiment_decisions | Preflight aborts |
| MC-04 | Orphan judgements | Preflight aborts |
| MC-05 | All IS NULL counts = 0 after backfill | Postflight passes |
| MC-06 | Non-zero IS NULL count after backfill | Postflight aborts |

### §5.8.4 Deterministic re-run and resumability

- Each backfill UPDATE uses `WHERE ... IS NULL` guards, so re-running after partial completion fills only remaining rows.
- DDL is not blindly re-runnable; the `down_revision` check prevents re-application after commit.
- On PostgreSQL, the entire migration runs in one transaction; failure rolls back everything.
- On SQLite, batch DDL is not atomic; recovery from partial failure requires backup restoration.

---

## §5.9 API contract tests

### §5.9.1 Full command matrix

Every mutation endpoint is tested with the following dimensions. Rows below list the command-specific values; shared dimensions (Idempotency-Key, replay, tenant 404, permission denial, workspace membership denial) apply to every row.

**Shared dimensions applied to every mutation command:**

| Dimension | Test | Expected |
|---|---|---|
| Missing `Idempotency-Key` | Omit header | `400 idempotency_key_required` |
| Same-key same-fingerprint replay | Repeat with same key | 200 with original outcome; no re-authorization; no new side effects |
| Same-key different-fingerprint | Different body, same key | `409 idempotency_key_reused` |
| Different-key same-operation | New key, same body | Full current guards re-evaluated |
| Tenant-scoped 404 | Wrong workspace (signed context for different workspace) | 404 |
| Permission denial | Missing required permission | 403 |
| Workspace membership denial | Non-member actor | 403 |

**Per-command matrix:**

| Command | Path | Success | If-Match | Stale revision/sequence | Schema dispatch | Exclusive wrong-schema |
|---|---|---|---|---|---|---|
| Create | `POST /api/experiments` | 201 | — | — | — | Legacy shape → `422 legacy_create_shape_retired` |
| Add entry | `POST /api/experiments/{id}/entries` | 201 | `revision` | `409 revision_conflict` | Schema-2 exclusive | `409 schema_version_mismatch` |
| Update entry | `PUT /api/experiments/{id}/entries/{eid}` | 200 | `revision` | `409 revision_conflict` | Schema-2 exclusive | `409 schema_version_mismatch` |
| Remove entry | `DELETE /api/experiments/{id}/entries/{eid}` | 200 | `revision` | `409 revision_conflict` | Schema-2 exclusive | `409 schema_version_mismatch` |
| Update context | `PUT /api/experiments/{id}/context` | 200 | `revision` | `409 revision_conflict` | Schema-2 exclusive | `409 schema_version_mismatch` |
| Freeze | `POST /api/experiments/{id}/freeze` | 201 | `revision` | `409 revision_conflict` | Shared URL (internal dispatch) | — |
| Bind candidate | `POST /api/experiments/{id}/candidates/{cid}/bind` | 201 | `revision` | `409 revision_conflict` | Schema-2 exclusive | `409 schema_version_mismatch` |
| Start | `POST /api/experiments/{id}/start` | 201 | `revision` | `409 revision_conflict` | Schema-2 exclusive | `409 schema_version_mismatch` |
| Judge | `POST /api/experiments/{id}/judge` | 201 | `revision` | `409 revision_conflict` | Shared URL (internal dispatch) | — |
| Confirm | `POST /api/experiments/{id}/proposals/{pid}/confirm` | 201 | `proposal_sequence` | `409 sequence_conflict` | Schema-2 exclusive | `409 schema_version_mismatch` |
| Close | `POST /api/experiments/{id}/close` | 201 | `revision` | `409 revision_conflict` | Shared URL (internal dispatch) | — |
| set_verdict | Legacy endpoint | `410 verdict_endpoint_retired` | — | — | All experiments | — |

### §5.9.2 Command-specific error codes

| Test ID | Endpoint | Scenario | Expected status | Error code |
|---|---|---|---|---|
| AS-01 | `POST /api/experiments/{id}/freeze` | Gate 1 failure | 422 | gate-specific code |
| AS-02 | `POST /api/experiments/{id}/freeze` | Candidate count > 100 | 409 | `candidate_limit_exceeded` |
| AS-03 | `POST /api/experiments/{id}/start` | Active invalidation | 409 | `eligibility_conflict` |
| AS-04 | `POST /api/experiments/{id}/start` | QC pending/running | 409 | `qc_in_progress` |
| AS-05 | `POST /api/experiments/{id}/proposals/{pid}/confirm` | Active irreversible invalidation | 409 | `proposal_invalidated` |
| AS-06 | `POST /api/experiments/{id}/proposals/{pid}/confirm` | All inconclusive | 409 | `no_material_verdicts` |
| AS-07 | `POST /api/experiments/{id}/proposals/{pid}/confirm` | Reversible invalidation active | 409 | `eligibility_conflict` |
| AS-08 | `POST /api/experiments/{id}/close` | `invalidated` without irreversible event | 409 | `no_active_invalidation` |
| AS-09 | `POST /api/experiments/{id}/close` | `completed` without judgement | 409 | `no_judgements` |
| AS-10 | `POST /api/experiments/{id}/close` | `abandoned_before_start` from running | 409 | wrong state |
| AS-11 | Any mutation | Missing `If-Match` where required | 428 | `precondition_required` |
| AS-12 | `POST /api/experiments/{id}/freeze` | Candidate count < 2 | Per §A9 | `insufficient_candidates_after_resolution` |

### §5.9.3 Pagination and bounds

Every collection endpoint is cursor-paginated and bounded:

| Test ID | Endpoint | Scenario | Expected |
|---|---|---|---|
| PG-01 | `GET /api/experiments/{id}/judgements` | Page 1 | Cursor-paginated; bounded page size |
| PG-02 | `GET /api/experiments/{id}/judgements` | Follow cursor to page 2 | Consistent ordering; no duplicates; no gaps |
| PG-03 | `GET /api/experiments/{id}/proposals` | Empty set | Empty list; no cursor |
| PG-04 | `GET /api/experiments/{id}/decisions` | Page 1 | Cursor-paginated; bounded page size |
| PG-05 | `GET /api/experiments/{id}/decisions` | Follow cursor | Consistent ordering; no duplicates |
| PG-06 | `GET /api/experiments/{id}/eligibility-events` | Ordering | `(observed_at ASC, id ASC)` |
| PG-07 | `GET /api/experiments/{id}/verdicts` | Current projections | One per context key; cursor-paginated |
| PG-08 | Any collection | Invalid cursor | 400 |
| PG-09 | Any collection | Authorization (wrong workspace) | 404 |

### §5.9.4 Response hashes and content addressing

| Test ID | Response field | Expected |
|---|---|---|
| RH-01 | `frozen_snapshot_hash` | Matches `_sha256(_canonical_bytes(frozen_snapshot_json))` |
| RH-02 | `proposal_hash` | Matches canonical computation |
| RH-03 | `eligibility_context_hash` | Matches 4-input formula |
| RH-04 | `last_eligibility_event_cursor` | Points to most recent event |

### §5.9.5 ETag and If-Match

| Test ID | Scenario | Expected |
|---|---|---|
| ET-01 | Missing `If-Match` on freeze | `428 precondition_required` |
| ET-02 | Correct `If-Match` revision | Operation proceeds |
| ET-03 | Stale `If-Match` revision | `409 revision_conflict` |
| ET-04 | Confirm with correct proposal sequence | Operation proceeds |
| ET-05 | Confirm with stale proposal sequence | `409 sequence_conflict` |
| ET-06 | GET response includes ETag | ETag present in response headers |
| ET-07 | Missing `If-Match` on bind | `428 precondition_required` |
| ET-08 | Missing `If-Match` on judge | `428 precondition_required` |
| ET-09 | Missing `If-Match` on start | `428 precondition_required` |
| ET-10 | Missing `If-Match` on close | `428 precondition_required` |
| ET-11 | Missing `If-Match` on draft edits | `428 precondition_required` |

### §5.9.6 Normative endpoint tests (addendum)

The following tests cover the normative addendum endpoints and are part of the complete design's mandatory test suite.

| Test ID | Endpoint | Scenario | Expected | Addendum |
|---|---|---|---|---|
| PA-01 | `GET /api/experiments` | List experiments in workspace | 200; cursor-paginated; tenant-scoped | §A1 |
| PA-02 | `GET /api/experiments` | Schema-version filter | Only matching rows | §A1 |
| PA-03 | `GET /api/experiments` | Cross-workspace isolation | Empty for other workspace | §A1 |
| PA-04 | `GET /api/experiments/{id}/timeline` | Full lifecycle timeline | Events in stable total order | §A2 |
| PA-05 | `GET /api/experiments/{id}/timeline` | Cursor pagination | No duplicates; no gaps | §A2 |
| PA-06 | `GET /api/experiments/{id}/timeline` | No unbounded evidence | `summary_json` bounded | §A2 |
| PA-07 | `POST /api/experiments/{eid}/decisions/{did}/next-round` | Valid next-round from material Decision | 201; GenerationDraft + NextRoundRecord + timeline event | §A6 |
| PA-08 | Judge/confirm/close lifecycle | No auto-draft on judge/confirm/close | No GenerationDraft after these operations | §A6 |
| PA-09 | `POST /api/experiments/{eid}/decisions/{did}/pattern-candidates` | Create pattern candidate from material Decision | 201; record created with `source_concepts_hash` and `concept_count` | §A7 |
| PA-10 | `POST /api/experiments/{eid}/decisions/{did}/pattern-candidates` | Inconclusive Decision rejected | Service rejection (not material) | §A7 |
| PA-11 | List/timeline page-size out of bounds | `?page_size=0` or `?page_size=999` | `422 invalid_page_size` | §A1, §A2 |
| PA-12 | Timeline signed cursor tampered | Modified cursor token | 400 invalid cursor | §A2 |

---

## §5.10 Controlled Mock and provenance isolation

### §5.10.1 Execution mode enforcement

| Test ID | Scenario | Expected |
|---|---|---|
| MP-01 | All candidates `execution_mode='mock'` | Freeze succeeds; mode recorded in Version |
| MP-02 | Mixed `mock` + `byok` candidates | Gate 12 rejects (mode uniformity) |
| MP-03 | Mock experiment: truth evaluation | NOT exempted; truth-dependency gate applies |
| MP-04 | Mock evidence in `eligibility_evidence_json` | `execution_mode` field present in evidence |
| MP-05 | All candidates `execution_mode='sandbox'` | Freeze succeeds; mode recorded in Version |

### §5.10.2 Provenance isolation

Mock/Sandbox and real evidence remain isolated and cannot enter real Pattern/Localization/Scale outcomes. The `verdict` column (not `verdict_value`) uses the CHECK constraint `IN ('win', 'learn', 'lose')` on `contextual_verdict_projections`. The `execution_mode` is on `experiment_version_candidates`, not on verdict projections; downstream consumers obtain it by joining through immutable ExperimentVersion → Candidate lineage.

| Test ID | Scenario | Expected |
|---|---|---|
| PI-01 | Mock candidate `source_kind` recorded | `source_kind` CHECK enforced: `generation\|upload\|reuse\|manual` |
| PI-02 | Mock candidate `source_evidence_json` | Contains provenance chain |
| PI-03 | Mock verdict projection: `verdict` column | Column is `verdict` (not `verdict_value`); CHECK `IN ('win', 'learn', 'lose')` |
| PI-04 | Mock execution_mode through lineage | `execution_mode` obtained by joining ExperimentVersion → experiment_version_candidates; value is `mock` | 
| PI-05 | Sandbox execution_mode through lineage | Same as PI-04 for sandbox; value is `sandbox` |
| PI-06 | Mock Decision: no `inconclusive` in material verdicts | Same `inconclusive` exclusion applies to mock |
| PI-07 | Mock/Sandbox evidence isolated from real experiment | Mock experiment's Decisions/projections do not appear in real experiment queries |

### §5.10.3 No-unlock-side-effect proof

Issue #22 proves that no Localization or Scale unlock action is invoked or enqueued by any Issue #22 code path. Downstream Localization (#24) and Scale (#25) consumption gates are out of scope; they read verdict projections and enforce their own rules.

| Test ID | Scenario | Expected |
|---|---|---|
| UB-01 | Confirmation with win/learn/lose (byok) | Side-effect spy: zero calls to any Localization/Scale unlock interface |
| UB-02 | Confirmation with mock execution_mode | Zero calls to unlock |
| UB-03 | Confirmation with sandbox execution_mode | Zero calls to unlock |
| UB-04 | Close (completed) | Zero calls to unlock |
| UB-05 | Pattern creation | Zero calls to unlock |
| UB-06 | Next-round action | Zero calls to unlock |

**Excluded from Issue #22**: Meta Sandbox orchestration (#23) and browser pages (#28). Export/manifest format is not an Issue #22 deliverable; Issue #22 proves that verdict projections carry sufficient provenance fields (`execution_mode` through lineage, `source_kind`, `source_evidence_json`) for downstream consumers to filter.

---

## §5.11 CI gates, test suites, and "done" definition

### §5.11.1 Test suites and commands

Commands are executable against repository conventions (`tests/test_*.py`, `python -m pytest -q`, `make verify`, psycopg (sync), PostgreSQL 16).

| Suite | Command | Scope | Gate |
|---|---|---|---|
| Default (SQLite + unit) | `make verify` | Unit, property, domain (SQLite), migration (SQLite) | PR merge gate |
| PostgreSQL domain + integration | `python -m pytest tests/test_experiment_decisions_pg.py -q` | §5.6, domain (PG), API | `domain-rls` CI job |

The PostgreSQL suite runs in the existing `domain-rls` CI topology with a PostgreSQL 16 service. Required environment: `MARKETAI_PG_TEST_URL` and role fixtures from `deploy/postgres/apply-roles.sh`.

PostgreSQL tests **fail** (not skip) when `MARKETAI_PG_TEST_URL` is absent in the `domain-rls` job. This ensures the PG suite cannot silently degrade to SQLite-only.

`make verify` alone is the SQLite/default gate. It does not prove PostgreSQL role, RLS, trigger, signed-context, or grant guarantees. Both gates must pass for "done."

### §5.11.2 Required artifacts

| Artifact | Source | Content |
|---|---|---|
| Migration revision | Alembic history | Single revision `p1a2b3c4d5e6` with `down_revision = n1a2b3c4d5e6` |
| JUnit XML | CI-generated by pytest (`--junitxml`) | Test results per suite; no secrets or PII |
| Issue #22 manifest | CI-generated JSON | Schema below |

**Issue #22 manifest schema** (machine-readable, CI-generated):

```json
{
  "artifact_schema_version": "1",
  "commit_sha": "<git rev-parse HEAD>",
  "migration_revision": "p1a2b3c4d5e6",
  "migration_head": "<alembic current>",
  "dialect": "postgresql|sqlite",
  "db_version": "PostgreSQL 16.x|SQLite 3.x",
  "role": "<test role used>",
  "deterministic_seed": "<hypothesis seed>",
  "fixed_clock_epoch": "<epoch>",
  "suites": [
    {
      "suite_id": "<suite name>",
      "tests": [
        {
          "test_id": "<§5.x test ID>",
          "result": "pass|fail",
          "canonical_hash": "<if applicable>",
          "section4_check": "<§4.11 number if mapped>"
        }
      ]
    }
  ]
}
```

No secrets, credentials, raw sensitive evidence, or PII in the manifest. Retention: uploaded as CI artifact with default CI retention policy.

### §5.11.3 Deterministic seeds, clocks, and IDs

- **Clock**: `deterministic_clock` fixture (§5.1.2); no `datetime.now()` in tested code paths.
- **IDs**: seeded generator producing ordered `test-{entity}-{seq}` IDs.
- **Random seeds**: Hypothesis profiles use `@settings(database=None, derandomize=True)` for CI reproducibility. Local runs may use random seeds.
- **JSON key ordering**: `sort_keys=True` in canonical encoder; tests verify ordering explicitly.

### §5.11.4 Flake policy

- Zero tolerance for flaky tests. A failure is reproducible from recorded seed, clock epoch, and interleaving.
- No arbitrary reruns, no silent retry annotations, no `sleep`-based races.
- Concurrency tests (§5.6.9) use sync psycopg + `threading.Barrier` synchronization.
- Quarantine: if a test must be quarantined, it requires an owner, linked issue, expiry date, and remains a release blocker. Quarantine cannot make a required gate green.

### §5.11.5 Structural bounds (hard, PR-blocking)

These are structural invariants enforced at the database or service level. They are always tested and always block PR merge.

| Bound | Enforcement | Test |
|---|---|---|
| Candidate count > 100 | `409 candidate_limit_exceeded` at freeze | FG-17 |
| Candidate count < 2 | `insufficient_candidates_after_resolution` at freeze (HTTP status per §A9) | FG-16 |
| Page size bounded | Pagination enforces max page size | PG-01..PG-07 |
| QC attempt cap | `max_qc_attempts_per_asset_version` at attempt creation | QC-12 |
| Bounded evidence | Counts + canonical set hashes; no unbounded ID lists | BE-01..BE-05 |
| Query result bounds | Collection queries bounded by pagination | PG-01..PG-09 |

### §5.11.6 Timed smoke budgets (CI-informational, scheduled/release tier optional)

Timed bounds are informational. Exceeding them triggers investigation, not automatic merge-block. For any timed bound promoted to a blocker, the specification must include fixture size, machine class, warm-up, repetitions/statistic, timeout, and whether PR or scheduled tier owns it.

| Operation | Budget | Fixture size | Tier |
|---|---|---|---|
| Migration upgrade (empty DB) | < 30s | Empty | PR (informational) |
| Migration upgrade (populated) | < 10 min | 10K experiments, 100K entries | Scheduled (not PR) |
| Canonical hash of 100-candidate Version | < 100ms | 100 candidates | PR (informational) |
| Eligibility projection rebuild | < 5s | 1K events | PR (informational) |
| Full workspace erasure | < 60s | 1K experiments | Scheduled (not PR) |

### §5.11.7 Security-negative tests

| Test ID | Scenario | Expected |
|---|---|---|
| SN-01 | Readonly role attempts INSERT | Permission denied |
| SN-02 | API role attempts DELETE on append-only table | Permission denied (API role lacks DELETE grant; privilege check fails before trigger) |
| SN-03 | API role attempts `SET ROLE marketai_projection_writer` | Permission denied |
| SN-04 | API role sets erasure GUC + DELETE | `pg_has_role` rejection |
| SN-05 | Non-erasure-principal invokes erasure procedure | Permission denied (no EXECUTE grant) |
| SN-06 | Cross-workspace read via RLS (signed context mismatch) | Empty result set |
| SN-07 | Cross-workspace write via RLS (signed context mismatch) | RLS policy violation |
| SN-08 | Direct SQL bypass of schema-version CHECK | CHECK violation |
| SN-09 | Stale authorization epoch in signed context | `marketai_context_is_current()` returns false; operations fail |
| SN-10 | Forged context signature | `marketai_context_is_current()` returns false; operations fail |

### §5.11.8 Definition of "implementation ready"

Implementation may begin when the complete design candidate — Sections 1–5, the approved-section delta, the normative addendum, and the acceptance traceability — receives holistic final-design approval. That approval changes `implementation_ready` to `true` but does not itself authorize or execute implementation.

A task plan mapping each Section 5 test to a code module and test file is a post-approval implementation-planning artifact, not a design-readiness gate. The migration revision registration and application are part of implementation.

**Current status**: `implementation_ready = true`. Holistic final-design approval granted 2026-09-03. Complete design SHA-256: `1210427eb9f6b479b84f167d435c7c354785c1f4c9b1d6a3b60f49245f902f8e` (5,471 lines). There are zero unresolved product-semantic decisions and zero known design gaps.

### §5.11.9 Definition of "done"

Issue #22 is done when:
1. `make verify` passes (SQLite/default gate).
2. The `domain-rls` PostgreSQL CI job passes (PG gate).
3. Every GitHub Issue #22 acceptance criterion (AC-1 through AC-7, §5.2) has a corresponding test that passes.
4. Every §4.11 acceptance check (1–88) has a corresponding test that passes. The manifest maps each check number to a test ID. No silent skips.
5. Every approved addendum contract and its tests (§5.14 traceability matrix) pass. Once approved, all listed contracts/tests are mandatory for Issue #22 completion.
6. The migration upgrades cleanly from `n1a2b3c4d5e6` on both dialects.
7. The migration downgrades cleanly when no non-re-derivable data exists. If non-re-derivable data exists, downgrade is blocked; recovery is forward-fix or verified backup restoration.
8. Migration data-conservation counts and checksums are recorded in the manifest.
9. All security-negative tests pass on real PostgreSQL.
10. All structural bounds (§5.11.5) are enforced.
11. No `TODO`, `FIXME`, or `HACK` comments remain in Issue #22-touched migration or test files. Unrelated repository TODOs do not block this issue.
12. The PR references Issue #22 and records: migrations, test evidence, operational impact, and remaining risks.
13. The Issue remains open until the PR is merged.
14. Explicit Issue #22 acceptance scope has not been deferred without a formal amendment of Issue #22 with explicit approval. Naming a future issue alone does not satisfy this requirement.

---

## §5.12 Unfinished-marker check

At PR time, scan only Issue #22-touched files (migration, domain service, test modules) for `TODO`, `FIXME`, `HACK`. Unrelated repository files are out of scope.

---

## §5.13 §4.11 migration-check traceability matrix

Every §4.11 check (1–88) mapped to its authoritative test. The "Test ID" column references tests defined in §5.3–§5.9 above.

| §4.11 | Description | Test ID(s) | Layer | Fixture |
|---|---|---|---|---|
| 1 | Clean upgrade PostgreSQL | MU-01 | Migration | Clean DB |
| 2 | Clean upgrade SQLite | MU-02, SQ-03 | Migration | Clean DB |
| 3 | Downgrade empty-schema-2 both dialects | MD-01, MD-02, SQ-04 | Migration | Clean DB |
| 4 | Downgrade with non-re-derivable data aborts | MD-03..MD-07 | Migration | Populated DB |
| 5 | Downgrade preflights duplicate detection | MD-08, MD-09 | Migration | Duplicate-seeded DB |
| 6 | Downgrade restores #21 trigger | MD-10 | Migration | Post-downgrade DB |
| 7 | Child workspace_id matches parent | MU-04 | Migration | Legacy data |
| 8 | Cross-workspace FK insert fails | FK-01 | Integration-PG | pg_roles |
| 9 | All workspace_id IS NULL = 0 | MC-05 | Migration | Post-backfill |
| 10 | 3-col lineage FK: mismatched event | FK-02 | Integration-PG | frozen_experiment |
| 11 | Ad bindings CHECKs | PU-01, PU-02 | Integration-PG | pg_roles |
| 12 | Judgements bidirectional CHECK | PU-03 | Integration-PG | pg_roles |
| 13 | Decisions full CASE CHECK | PU-04, PU-05 | Integration-PG | pg_roles |
| 14 | record_schema_version CHECK | PU-06 | Integration-PG | pg_roles |
| 15 | experiments.record_schema_version immutable | WO-04..WO-06 | Integration-PG | schema2_experiment |
| 16 | experiments.started_at set-once | WO-07, WO-08 | Integration-PG | running_experiment |
| 17 | ad_bindings.record_schema_version immutable | WO-09, WO-10 | Integration-PG | frozen_experiment |
| 18 | ProductProfile existing rows NULL | MU-03 | Migration | Legacy data |
| 19 | ProductProfile partial unique | PU-07 | Integration-PG | pg_roles |
| 20 | ProductProfile cross-workspace FK | FK-01 | Integration-PG | second_workspace |
| 21 | ProductProfile set-once trigger | WO-01..WO-03 | Integration-PG | pg_roles |
| 22 | Hash equality across dialects | HD-01, SQ-05 | Unit | canonical_test_vector |
| 23 | Truth hash reproducibility | HD-02, HD-03, MU-06 | Unit + Migration | — |
| 24 | QC evidence hash reproducibility | HD-04, MU-08 | Unit + Migration | — |
| 25 | Normalized columns equal snapshot | SI-01, LC-06 | Unit + Domain | frozen_experiment |
| 26 | _canonical_json rejects float | CE-06, CE-07 | Unit | — |
| 27 | Encoder rejects naive datetime; preserves precision | CE-10..CE-12 | Unit | — |
| 28 | Nested test vectors | CE-01..CE-16 | Unit | canonical_test_vector |
| 29a | Decimal plain notation + negative zero | CE-03, CE-04 | Unit | — |
| 29b | Non-string dict key rejection | CE-08, CE-09 | Unit | — |
| 30 | ECH: frozen_snapshot_hash sensitivity | ECH-01 | Unit | — |
| 31 | ECH: version projection head sensitivity | ECH-02 | Unit | — |
| 32 | ECH: recheck evidence sensitivity | ECH-03 | Unit | — |
| 33 | ECH: candidate set add/remove | ECH-04, ECH-05 | Unit | — |
| 34 | ECH: swap candidate hashes | ECH-06 | Unit | — |
| 35 | ECH: ordering invariance | ECH-07 | Unit | — |
| 36 | Delayed-delivery rebuild equivalence | EL-05 | Domain | frozen_experiment |
| 37 | Equal-wall-clock rebuild equivalence | EL-06 | Domain | deterministic_clock |
| 38 | Rebuild verifies all projection fields | EL-07 | Domain | frozen_experiment |
| 39 | Baseline head_hash non-NULL | EL-08 | Domain | frozen_experiment |
| 40 | CAS rejects stale head_sequence | EL-09 | Domain | frozen_experiment |
| 41 | Proposal event sequence unique | DP-01, DP-09 | Domain | judged_experiment |
| 42 | Confirmation per-proposal unique | DP-03, DP-04 | Domain | judged_experiment |
| 43 | Idempotency-key unique | DP-05 | Domain | judged_experiment |
| 44 | Concurrent confirmation race | DP-06, DP-06a, DP-06b | Integration-PG | judged_experiment (sync psycopg + threading.Barrier) |
| 45 | Proposal: Judgement different experiment (4-col FK) | FK-03 | Integration-PG | frozen_experiment |
| 46 | Proposal: Judgement different version (4-col FK) | FK-04 | Integration-PG | frozen_experiment |
| 47 | Proposal: Version different experiment (3-col FK) | FK-05 | Integration-PG | frozen_experiment |
| 48 | Decision: candidate different version (3-col FK) | FK-06 | Integration-PG | confirmed_experiment |
| 49 | Decision: confirmation different proposal (3-col FK) | FK-07 | Integration-PG | confirmed_experiment |
| 50 | Decision: experiment_id ≠ Proposal's (5-col FK) | FK-08 | Integration-PG | confirmed_experiment |
| 51 | Decision: version_id ≠ Proposal's (5-col FK) | FK-09 | Integration-PG | confirmed_experiment |
| 52 | Decision: judgement_id ≠ Proposal's (5-col FK) | FK-10 | Integration-PG | confirmed_experiment |
| 53 | Verdict: IDs from different versions | LC-10 (service) | Domain | confirmed_experiment |
| 54 | Verdict: asset_version_id ≠ Candidate's | LC-10 (service) | Domain | confirmed_experiment |
| 55 | Verdict: product_version_id ≠ Version's | LC-10 (service) | Domain | confirmed_experiment |
| 56 | Verdict: context fields ≠ Version's | LC-10 (service) | Domain | confirmed_experiment |
| 57 | UPDATE on append-only tables | AO-01..N (dynamic) | Integration-PG | pg_roles (DML-capable role) |
| 58 | DELETE on append-only tables (non-erasure) | AO-(N+1)..2N (dynamic) | Integration-PG | pg_roles (DML-capable role) |
| 59 | Eligibility: UPDATE allowed, DELETE rejected | AO-M1, AO-M2 | Integration-PG | frozen_experiment |
| 60 | Verdict: API/worker DELETE denied; writer via function allowed | RG-07, RG-08, SF-07 | Integration-PG | pg_roles |
| 61 | Column write-once triggers | WO-01..12 | Integration-PG | pg_roles |
| 62 | Projection function: delete + replace | SF-07, SF-09 | Integration-PG | confirmed_experiment |
| 63 | Direct DELETE denied; no SET ROLE | RG-07, RG-08, RG-18 | Integration-PG | pg_roles |
| 64 | Projection function: tenant validation | SF-08 | Integration-PG | second_workspace |
| 65 | API/worker invokes function on replacement path | SF-07 | Integration-PG | confirmed_experiment |
| 66 | Shared function exists | TC-01 | Integration-PG | Post-migration |
| 67 | All append-only triggers exist (tgrelid) | TC-02 | Integration-PG | Post-migration |
| 68 | All write-once triggers exist | TC-03 | Integration-PG | Post-migration |
| 69 | No CREATE TRIGGER IF NOT EXISTS | TC-04 | Unit (source scan) | — |
| 70 | #21 trigger replaced | TC-05 | Integration-PG | Post-migration |
| 71 | Projection function: ownership + grants + SECDEF | SF-01..06, SF-15 | Integration-PG | Post-migration |
| 72 | SQLite JSON round-trip | SQ-01 | SQLite parity | — |
| 73 | SQLite QC evidence hash parity | SQ-02, MU-09 | SQLite parity | — |
| 74 | Erasure: full workspace | ER-01 | Integration-PG | confirmed_experiment |
| 75 | Erasure: cross-workspace isolation | ER-02 | Integration-PG | second_workspace |
| 76 | Non-erasure DELETE rejected | ER-03 | Integration-PG | pg_roles |
| 77 | RESTRICT FK blocks misordered erasure | ER-04 | Integration-PG | confirmed_experiment |
| 78 | API/worker erasure GUC bypass denied | ER-05 | Integration-PG | pg_roles |
| 79 | Audit receipt survives erasure | ER-06 | Integration-PG | confirmed_experiment |
| 80 | Erasure procedure OWNER BYPASSRLS | ER-07, ER-07a | Integration-PG | Post-migration |
| 81 | Bootstrap: NULL→false, non-NULL→true | MU-05 | Migration | Legacy data |
| 82 | Gate 13 fail-closed | TD-01 | Domain | schema2_experiment |
| 83 | Truth CHECKs | TD-05, TD-06 | Integration-PG | pg_roles |
| 84 | Truth write-once | TD-07, TD-10 | Integration-PG | pg_roles |
| 85 | Bootstrap rejection post-cutover | TD-08, TD-09 | Integration-PG | pg_roles |
| 86 | QC sequence uniqueness | QC-13 | Domain | — |
| 87 | QC remediation invariants | QC-06..12 | Domain | — |
| 88 | Active-attempt partial unique | QC-05, QC-22, PU-08 | Domain + Integration-PG | — |

---

## §5.14 Addendum traceability matrix

This matrix traces every addendum contract and test to a specific addendum item. All addendum items are normative additions in the complete design candidate. Their tests are mandatory for Issue #22 completion.

The table count and integration matrices (RLS, grants, triggers, erasure, migration) are NOT hard-coded. Addendum tables join the dynamic `APPEND_ONLY_TABLES` enumeration (§5.6.3), the RLS matrix (§5.6.5), the grant matrix (§5.6.6), the erasure dependency order (§5.6.8), and the migration order — the base count increases accordingly.

### §5.14.1 List endpoint (§A1)

| Contract | Test IDs | Layer | Status |
|---|---|---|---|
| `GET /api/experiments` with signed keyset cursor, page-specific ETag, no cross-request snapshot | AL-01..AL-14, PA-01..PA-03, PA-11 | Domain + API | Normative |
| Deterministic ordering, no duplicate delivery of unchanged rows in forward traversal | AL-01, AL-04, AL-08 | Domain | Normative |
| Workspace scoping via RLS | AL-06, PA-03 | Domain | Normative |
| Page-size `422 invalid_page_size` | AL-05, PA-11 | API | Normative |
| Schema-version filter | AL-03, PA-02 | Domain | Normative |
| Same-timestamp tie-breaking via `id DESC` | AL-08 | Domain | Normative |
| Concurrent insert weak consistency (may or may not appear) | AL-13 | Domain | Normative |
| Cursor reuse with changed filter rejected | AL-10 | API | Normative |
| Page-specific ETag + `If-None-Match` 304 | AL-11, AL-12 | API | Normative |

### §5.14.2 Timeline endpoint (§A2)

| Contract | Test IDs | Layer | Status |
|---|---|---|---|
| `experiment_timeline_heads` schema + lock class 7 | TL-08, TL-09, LO-05 | Domain + Integration-PG | Normative |
| `experiment_timeline_events` schema + append-only | TL-01..TL-15, PA-04..PA-06, PA-12 | Domain + API | Normative |
| Event vocabulary coverage | TL-03, TL-05, TL-15 | Domain | Normative |
| Signed cursor + sequence-bounded pagination | TL-02, TL-12, TL-14, PA-12 | API | Normative |
| Legacy backfill deterministic | TL-10 | Domain | Normative |
| Source-dedup unique | TL-11 | Integration-PG | Normative |
| RLS, FORCE RLS (both tables) | ATI-01, ATI-02 per table | Integration-PG | Normative |
| Grants: API SELECT+INSERT+UPDATE(heads only), no DELETE | ATI-08..ATI-10, ATI-29 | Integration-PG | Normative |
| Grants: Worker SELECT+INSERT only, no UPDATE/DELETE | ATI-11, ATI-30, ATI-31 | Integration-PG | Normative |
| Erasure: dependency order | ATI-16, ATI-17 | Integration-PG | Normative |
| Timeline heads: API UPDATE on `head_sequence` only | ATI-29, ATI-30 | Integration-PG | Normative |

### §5.14.3 Notification outbox (§A5)

| Contract | Test IDs | Layer | Status |
|---|---|---|---|
| `experiment_notification_outbox` schema + append-only trigger (no delivery columns) | NO-01..NO-18 | Domain + Integration-PG | Normative |
| Composite FK to `experiment_timeline_events` | NO-10, ATI-05 | Integration-PG | Normative |
| `UNIQUE(workspace_id, timeline_event_id, notification_type)` | NO-10 | Integration-PG | Normative |
| One-notification-per-domain-cause (`decision_confirmed` notifies; `decisions_materialized` and `proposal_invalidated` do not) | NO-03, NO-08, NO-09, NO-15 | Domain | Normative |
| Append-only trigger (all columns immutable) | NO-13, ATI-06 | Integration-PG | Normative |
| Replay proofs (4 replay classes) | NO-02, NO-04, NO-05, NO-06 | Domain | Normative |
| Bounded payload (`octet_length(payload_canonical_json) <= 4096`) + `payload_hash` | NO-12, NO-18 | Domain + Integration-PG | Normative |
| Canonical round-trip verification: invalid JSON or non-canonical bytes rejected | NO-16 | Domain | Normative |
| `payload_hash` mismatch rejected | NO-17 | Domain | Normative |
| UTF-8 multibyte 4096-byte boundary (both dialects) | NO-18 | Domain + Integration-PG | Normative |
| RLS, grants, erasure (API/Worker/Readonly/ProjectionWriter/Erasure) | ATI-01..ATI-04, ATI-08..ATI-11, ATI-14..ATI-17, ATI-20..ATI-23, ATI-30..ATI-41 | Integration-PG | Normative |

### §5.14.4 Next-round action (§A6)

| Contract | Test IDs | Layer | Status |
|---|---|---|---|
| `POST /api/experiments/{eid}/decisions/{did}/next-round` | NR-01..NR-27, PA-07, PA-08 | Domain + API | Normative |
| `ExperimentNextRoundRecord` with `generation_draft_id` UNIQUE + `qc_attempt_id` CHECK + `source_candidate_id` | NR-17, NR-18..NR-23 | Integration-PG + Domain | Normative |
| QC provenance: workspace + AssetVersion validation | NR-19..NR-21 | Domain | Normative |
| Cross-field lineage: Decision→Experiment, source_asset_version_id, source_candidate_id | NR-24, NR-25 | Domain | Normative |
| Composite lineage FKs: Decision 5-col, Candidate 4-col, QC-attempt 3-col | NR-26, NR-27, ATI-05, ATI-24..ATI-26 | Integration-PG | Normative |
| No auto-draft on judge/confirm/close | NR-10..NR-12, PA-08 | Domain | Normative |
| No Library template mutation | NR-13 | Domain | Normative |
| Append-only trigger | NR-15, ATI-06 | Integration-PG | Normative |
| Timeline event emitted | NR-01 | Domain | Normative |
| RLS, grants, erasure (API/Worker/Readonly/ProjectionWriter/Erasure) | ATI-01..ATI-04, ATI-08..ATI-11, ATI-14..ATI-17, ATI-20..ATI-23, ATI-30..ATI-41 | Integration-PG | Normative |

### §5.14.5 Pattern candidate (§A7)

| Contract | Test IDs | Layer | Status |
|---|---|---|---|
| `POST /api/experiments/{eid}/decisions/{did}/pattern-candidates` | PC-01..PC-22, PA-09, PA-10 | Domain + API | Normative |
| `ExperimentPatternCandidateRecord` with `UNIQUE(workspace_id, decision_id)` | PC-04, PC-05 | Integration-PG | Normative |
| `source_concepts_hash` equals persisted Candidate `concepts_hash` (no recomputation) | PC-16, PC-17, PC-18 | Domain | Normative |
| Cross-field lineage: candidate_id, Decision→Experiment | PC-19, PC-20 | Domain | Normative |
| Composite lineage FKs: Decision 5-col, Candidate 3-col | PC-21, PC-22, ATI-05, ATI-27, ATI-28 | Integration-PG | Normative |
| INSERT-or-load replay | PC-04, PC-05 | Domain | Normative |
| Execution-mode isolation | PC-10 | Domain | Normative |
| No Library write | PC-11 | Domain | Normative |
| Bounded evidence | PC-07 | Domain | Normative |
| Append-only trigger | PC-08, ATI-06 | Integration-PG | Normative |
| Timeline event emitted; no notification | PC-14, PC-15 | Domain | Normative |
| RLS, grants, erasure (API/Worker/Readonly/ProjectionWriter/Erasure) | ATI-01..ATI-04, ATI-08..ATI-11, ATI-14..ATI-17, ATI-20..ATI-23, ATI-30..ATI-41 | Integration-PG | Normative |

### §5.14.6 Unlock boundary (§A8)

| Contract | Test IDs | Layer | Status |
|---|---|---|---|
| No Localization/Scale unlock on any Issue #22 code path | UB-01..UB-06 | Domain | Normative |
| Side-effect spy covers: confirmation (all modes), close, next-round, pattern | UB-01..UB-06 | Domain | Normative |

### §5.14.7 Below-minimum HTTP status (§A9)

| Contract | Test IDs | Layer | Status |
|---|---|---|---|
| `insufficient_candidates_after_resolution` → `409` | FG-16, CC-13, AS-12 | Domain + API | Normative |

### §5.14.8 Table integration (§A10)

All addendum tables join the following dynamic matrices:

| Matrix | Effect | Integration point |
|---|---|---|
| `APPEND_ONLY_TABLES` registry | New append-only tables added; AO-* test count increases | §5.6.3 |
| RLS + FORCE RLS | RL-01..RL-04 enumerate all workspace-owned tables including addendum | §5.6.5 |
| Role/grant matrix | RG-* rows extended per §A10.3 (API, Worker, projection_writer, readonly separate) | §5.6.6 |
| Erasure dependency order | Extended leaf→root order per §A10.4 | §5.6.8 |
| Migration creation/drop | Root→leaf create; leaf→root drop per §A10.5 | §5.8 |
| Downgrade preflight | Non-re-derivable addendum data blocks downgrade per §A10.6 | §5.8.2 |
| SQLite guards | Service-level enforcement per §A10.7 | §5.7.3 |

### §5.14.9 Acceptance test matrix for addendum tables (§A10.8)

Each addendum table must pass the following ATI-* tests (defined in Addendum §A10.8):

| Test ID | Test category | Per table | Layer |
|---|---|---|---|
| ATI-01 | RLS ENABLE + FORCE | `pg_class` flags | Integration-PG |
| ATI-02 | RLS policy exists | `USING/WITH CHECK` with `marketai_context_is_current` | Integration-PG |
| ATI-03 | Cross-workspace SELECT | Empty result | Integration-PG |
| ATI-04 | Cross-workspace INSERT | RLS policy violation | Integration-PG |
| ATI-05 | Composite FK | Mismatched workspace → FK violation | Integration-PG |
| ATI-06 | Append-only trigger (where applicable) | UPDATE/DELETE → trigger rejection (via DML-capable role) | Integration-PG |
| ATI-07 | *(removed — outbox is now fully append-only, covered by ATI-06)* | — | — |
| ATI-08 | Grant: API SELECT, INSERT | Allowed | Integration-PG |
| ATI-09 | Grant: API UPDATE on append-only | Denied | Integration-PG |
| ATI-10 | Grant: API DELETE on append-only | Denied | Integration-PG |
| ATI-11 | Grant: Worker SELECT, INSERT | Allowed | Integration-PG |
| ATI-12 | *(removed — no delivery columns)* | — | — |
| ATI-13 | *(removed — no delivery columns)* | — | — |
| ATI-14 | Grant: Readonly SELECT | Allowed | Integration-PG |
| ATI-15 | Grant: Readonly INSERT/UPDATE/DELETE | Denied | Integration-PG |
| ATI-16 | Erasure: procedure deletes in order | All rows deleted | Integration-PG |
| ATI-17 | Erasure: misordered | RESTRICT FK blocks | Integration-PG |
| ATI-18 | SQLite append-only guard | Service rejects UPDATE/DELETE | Domain |
| ATI-19 | SQLite workspace scoping | Only own workspace visible | Domain |
| ATI-20 | Erasure caller: no direct DML on any experiment-domain table | SELECT/INSERT/UPDATE/DELETE denied on all tables | Integration-PG |
| ATI-21 | Procedure owner is not a runtime membership target | `pg_has_role(runtime, owner, 'member')` = false | Integration-PG |
| ATI-22 | API/Worker cannot invoke erasure procedure | EXECUTE denied | Integration-PG |
| ATI-23 | ProjectionWriter: no INSERT/UPDATE/DELETE on addendum evidence tables | Denied (SELECT only) | Integration-PG |
| ATI-24 | NextRound: Decision lineage FK mismatch (wrong version) | FK violation | Integration-PG |
| ATI-25 | NextRound: Candidate/asset_version FK mismatch | FK violation | Integration-PG |
| ATI-26 | NextRound: QC attempt wrong asset_version FK mismatch | FK violation | Integration-PG |
| ATI-27 | PatternCandidate: Decision lineage FK mismatch (wrong version) | FK violation | Integration-PG |
| ATI-28 | PatternCandidate: wrong-version candidate FK mismatch | FK violation | Integration-PG |
| ATI-29 | Grant: API UPDATE on timeline_heads | UPDATE `head_sequence` allowed | Integration-PG |
| ATI-30 | Grant: Worker UPDATE on all addendum tables | Denied (SELECT + INSERT only) | Integration-PG |
| ATI-31 | Grant: Worker DELETE on all addendum tables | Denied | Integration-PG |
| ATI-32 | Erasure caller: SELECT on every experiment-domain table | Denied (zero DML) | Integration-PG |
| ATI-33 | Erasure caller: no BYPASSRLS | `rolbypassrls` = false | Integration-PG |
| ATI-34 | Erasure procedure owner: BYPASSRLS present | `rolbypassrls` = true | Integration-PG |
| ATI-35 | Erasure procedure: call as erasure_principal succeeds | Owner DML deletes target workspace only | Integration-PG |
| ATI-36 | PUBLIC EXECUTE revoked on erasure procedure | `has_function_privilege` = false | Integration-PG |
| ATI-37 | Readonly EXECUTE on erasure procedure | Denied | Integration-PG |
| ATI-38 | ProjectionWriter EXECUTE on erasure procedure | Denied | Integration-PG |
| ATI-39 | No runtime role is member of erasure principal | `pg_has_role` = false for all 4 runtime roles | Integration-PG |
| ATI-40 | ProjectionWriter SELECT on every addendum table | SELECT succeeds without privilege error | Integration-PG |
| ATI-41 | Erasure caller: no EXECUTE on other SECDEF functions | EXECUTE on projection fn and others → denied | Integration-PG |


<!-- source-boundary: final-acceptance-traceability -->
# Issue #22 — Final Acceptance Traceability

This document maps every Issue #22 scope item and acceptance criterion to authoritative sections, commands/tables, test IDs, migration evidence, and Done gates. All addendum items are normative additions in the complete design candidate. There are zero known design gaps.

---

## 1. GitHub Issue #22 acceptance criteria → test coverage

| AC | Criterion | Authoritative sections | Test IDs | Migration evidence | Done gate |
|---|---|---|---|---|---|
| AC-1 | Draft edits before freeze; frozen inputs/hashes immutable | §1.2, §1.3, §3.3–§3.6, §4.2–§4.3 | AC1-01..AC1-14, IS-01..IS-05, AO-01..AO-N | ExperimentVersionRecord + Candidates write-once trigger | `make verify` + `domain-rls` |
| AC-2 | Non-Approved, withdrawn, stale-truth, mixed-mode, cross-workspace blocked | §1.5, §2.1–§2.4, §3.7 | AC2-01..AC2-14, FG-01..FG-20, RC-01..RC-09, TD-01..TD-13, FK-01..FK-10 | Eligibility gates + composite FKs + RLS | `make verify` + `domain-rls` |
| AC-3 | Same-concept duplication warns and requires resolution | §1.4, §3.7 Gate 15 | AC3-01..AC3-09, CC-01..CC-13 | ConceptResolutionRecord + duplicate_group_hash | `make verify` |
| AC-4 | Pure judge deterministic; DB/time/env not affecting | §1.12, §3.8 | AC4-01..AC4-15 | No migration (pure function) | `make verify` |
| AC-5 | Judge appends Judgement/Proposal only; permitted actor confirms | §1.7, §3.8–§3.9 | AC5-01..AC5-11, DP-01..DP-16 | DecisionProposal + StateEvent + Confirmation + Decision rows | `make verify` + `domain-rls` |
| AC-6 | Inconclusive never material; cannot unlock Localization/Scale | §1.10, §2.5, §3.9 | AC6-01..AC6-11, UB-01..UB-06, MP-01..MP-05, PI-01..PI-07 | verdict CHECK IN ('win','learn','lose') on projections | `make verify` |
| AC-7 | Replays never rewrite Judgements/Decisions or duplicate notifications | §3.2, Addendum §A4–§A5 | AC7-01..AC7-10, NO-01..NO-18 | Idempotency + outbox dedup unique | `make verify` + `domain-rls` |

---

## 2. §4.11 migration checks → test coverage

| Check | Description | Test ID(s) | Layer |
|---|---|---|---|
| 1 | Clean upgrade PostgreSQL | MU-01 | Migration |
| 2 | Clean upgrade SQLite | MU-02, SQ-03 | Migration |
| 3 | Downgrade empty both dialects | MD-01, MD-02, SQ-04 | Migration |
| 4 | Downgrade with non-re-derivable data aborts | MD-03..MD-07 | Migration |
| 5 | Downgrade preflights | MD-08, MD-09 | Migration |
| 6 | Downgrade restores #21 trigger | MD-10 | Migration |
| 7 | workspace_id backfill correctness | MU-04 | Migration |
| 8 | Cross-workspace FK | FK-01 | Integration-PG |
| 9 | workspace_id IS NULL = 0 | MC-05 | Migration |
| 10 | 3-col lineage FK | FK-02 | Integration-PG |
| 11 | Ad bindings CHECKs | PU-01, PU-02 | Integration-PG |
| 12 | Judgements CHECK | PU-03 | Integration-PG |
| 13 | Decisions CASE CHECK | PU-04, PU-05 | Integration-PG |
| 14 | record_schema_version CHECK | PU-06 | Integration-PG |
| 15 | record_schema_version immutable | WO-04..WO-06 | Integration-PG |
| 16 | started_at set-once | WO-07, WO-08 | Integration-PG |
| 17 | ad_bindings schema immutable | WO-09, WO-10 | Integration-PG |
| 18 | ProductProfile existing NULL | MU-03 | Migration |
| 19 | ProductProfile partial unique | PU-07 | Integration-PG |
| 20 | ProductProfile cross-workspace FK | FK-01 | Integration-PG |
| 21 | ProductProfile set-once trigger | WO-01..WO-03 | Integration-PG |
| 22 | Hash equality across dialects | HD-01, SQ-05 | Unit |
| 23 | Truth hash reproducibility | HD-02, HD-03, MU-06 | Unit + Migration |
| 24 | QC evidence hash reproducibility | HD-04, MU-08 | Unit + Migration |
| 25 | Normalized columns = snapshot | SI-01, LC-06 | Unit + Domain |
| 26 | Float rejection | CE-06, CE-07 | Unit |
| 27 | Datetime precision + naive rejection | CE-10..CE-12 | Unit |
| 28 | Nested test vectors | CE-01..CE-16 | Unit |
| 29a | Decimal plain notation + negative zero | CE-03, CE-04 | Unit |
| 29b | Non-string dict key rejection | CE-08, CE-09 | Unit |
| 30 | ECH frozen_snapshot_hash sensitivity | ECH-01 | Unit |
| 31 | ECH version projection head | ECH-02 | Unit |
| 32 | ECH recheck evidence | ECH-03 | Unit |
| 33 | ECH candidate set add/remove | ECH-04, ECH-05 | Unit |
| 34 | ECH swap candidate hashes | ECH-06 | Unit |
| 35 | ECH ordering invariance | ECH-07 | Unit |
| 36 | Delayed-delivery rebuild equivalence | EL-05 | Domain |
| 37 | Equal-wall-clock rebuild equivalence | EL-06 | Domain |
| 38 | Rebuild verifies all fields | EL-07 | Domain |
| 39 | Baseline head_hash non-NULL | EL-08 | Domain |
| 40 | CAS rejects stale head_sequence | EL-09 | Domain |
| 41 | Proposal event sequence unique | DP-01, DP-09 | Domain |
| 42 | Confirmation per-proposal unique | DP-03, DP-04 | Domain |
| 43 | Idempotency-key unique | DP-05 | Domain |
| 44 | Concurrent confirmation race | DP-06, DP-06a, DP-06b | Integration-PG |
| 45 | Proposal: Judgement different experiment | FK-03 | Integration-PG |
| 46 | Proposal: Judgement different version | FK-04 | Integration-PG |
| 47 | Proposal: Version different experiment | FK-05 | Integration-PG |
| 48 | Decision: candidate different version | FK-06 | Integration-PG |
| 49 | Decision: confirmation different proposal | FK-07 | Integration-PG |
| 50 | Decision: experiment_id ≠ Proposal's | FK-08 | Integration-PG |
| 51 | Decision: version_id ≠ Proposal's | FK-09 | Integration-PG |
| 52 | Decision: judgement_id ≠ Proposal's | FK-10 | Integration-PG |
| 53 | Verdict: IDs from different versions | LC-10 | Domain |
| 54 | Verdict: asset_version_id mismatch | LC-10 | Domain |
| 55 | Verdict: product_version_id mismatch | LC-10 | Domain |
| 56 | Verdict: context fields mismatch | LC-10 | Domain |
| 57 | UPDATE on append-only tables | AO-01..N (dynamic) | Integration-PG |
| 58 | DELETE on append-only (non-erasure) | AO-(N+1)..2N (dynamic) | Integration-PG |
| 59 | Eligibility: UPDATE allowed, DELETE rejected | AO-M1, AO-M2 | Integration-PG |
| 60 | Verdict: DELETE denied/writer via function | RG-07, RG-08, SF-07 | Integration-PG |
| 61 | Column write-once triggers | WO-01..WO-12 | Integration-PG |
| 62 | Projection function: delete + replace | SF-07, SF-09 | Integration-PG |
| 63 | Direct DELETE denied; no SET ROLE | RG-07, RG-08, RG-18 | Integration-PG |
| 64 | Projection function: tenant validation | SF-08 | Integration-PG |
| 65 | API/worker invokes function | SF-07 | Integration-PG |
| 66 | Shared trigger function exists | TC-01 | Integration-PG |
| 67 | All append-only triggers exist | TC-02 | Integration-PG |
| 68 | All write-once triggers exist | TC-03 | Integration-PG |
| 69 | No CREATE TRIGGER IF NOT EXISTS | TC-04 | Unit (source scan) |
| 70 | #21 trigger replaced | TC-05 | Integration-PG |
| 71 | Projection function properties | SF-01..SF-06, SF-15 | Integration-PG |
| 72 | SQLite JSON round-trip | SQ-01 | SQLite parity |
| 73 | SQLite QC evidence hash parity | SQ-02, MU-09 | SQLite parity |
| 74 | Erasure: full workspace | ER-01 | Integration-PG |
| 75 | Erasure: cross-workspace isolation | ER-02 | Integration-PG |
| 76 | Non-erasure DELETE rejected | ER-03 | Integration-PG |
| 77 | RESTRICT FK blocks misordered erasure | ER-04 | Integration-PG |
| 78 | API/worker erasure bypass denied | ER-05 | Integration-PG |
| 79 | Audit receipt survives erasure | ER-06 | Integration-PG |
| 80 | Erasure procedure OWNER BYPASSRLS | ER-07, ER-07a | Integration-PG |
| 81 | Bootstrap truth backfill | MU-05 | Migration |
| 82 | Gate 13 fail-closed | TD-01 | Domain |
| 83 | Truth CHECKs | TD-05, TD-06 | Integration-PG |
| 84 | Truth write-once | TD-07, TD-10 | Integration-PG |
| 85 | Bootstrap rejection post-cutover | TD-08, TD-09 | Integration-PG |
| 86 | QC sequence uniqueness | QC-13 | Domain |
| 87 | QC remediation invariants | QC-06..QC-12 | Domain |
| 88 | Active-attempt partial unique | QC-05, QC-22, PU-08 | Domain + Integration-PG |

**Coverage**: All 88 checks mapped. No gaps.

---

## 3. Scope items → coverage status

| Scope item | Status | Authoritative source | Test IDs | Gaps |
|---|---|---|---|---|
| List API | Normative | Addendum §A1 + delta D-S3-01 | AL-01..AL-14, PA-01..PA-03, PA-11 | None |
| Detail API | Approved | §3.1 | Covered by LC-*, SD-* | None |
| Timeline API | Normative | Addendum §A2 + delta D-S2-01, D-S3-02 | TL-01..TL-15, PA-04..PA-06, PA-12, LO-05 | None |
| Cancel API | Approved mapping | §3.10 via §A3 | LC-11..LC-14, AS-08..AS-10 | None |
| Replay taxonomy | Approved mapping | §3.2 + §A4 + §A5 | AC7-01..AC7-10 | None |
| Notification dedup | Normative | Addendum §A5 + delta D-S2-02 | NO-01..NO-18, ATI-01..ATI-41 | None |
| Next-round draft | Normative | Addendum §A6 + deltas D-S3-03, D-S4-01 | NR-01..NR-27, PA-07, PA-08, ATI-01..ATI-41 | None |
| Pattern candidate | Normative | Addendum §A7 + deltas D-S3-04, D-S4-01 | PC-01..PC-22, PA-09, PA-10, ATI-01..ATI-41 | None |
| Unlock boundary | Normative | Addendum §A8 | UB-01..UB-06, AC6-08..AC6-11 | None |
| Below-min HTTP status | Normative | Addendum §A9 + delta D-S3-05 | FG-16, CC-13, AS-12 | None |
| Table integration | Normative | Addendum §A10 + deltas D-S4-02..D-S4-08 | ATI-01..ATI-41 | None |
| Optimistic revisions | Approved | §3.2 | OC-01..OC-08, ET-01..ET-11 | None |
| Reason codes/evidence | Approved | §2.3, §2.5, §3.10 | BE-01..BE-05, TD-*, QC-* | None |

---

## 4. Tables and migration evidence

### 4.1 New tables (Issue #22 core)

| Table | Type | RLS | Trigger | Erasure order | Migration step |
|---|---|---|---|---|---|
| `experiment_versions` | Write-once | Yes + FORCE | Append-only | After decisions | §4.2 |
| `experiment_version_candidates` | Write-once | Yes + FORCE | Append-only | After versions | §4.2 |
| `experiment_version_candidate_concepts` | Write-once | Yes + FORCE | Append-only | After candidates | §4.2 |
| `experiment_version_concept_resolutions` | Write-once | Yes + FORCE | Append-only | After versions | §4.2 |
| `decision_proposals` | Write-once header | Yes + FORCE | Append-only | After judgements | §4.2 |
| `decision_proposal_state_events` | Append-only | Yes + FORCE | Append-only | After proposals | §4.2 |
| `decision_confirmations` | Write-once | Yes + FORCE | Append-only | After proposals | §4.2 |
| `experiment_eligibility_events` | Append-only | Yes + FORCE | Append-only | After versions | §4.2 |
| `experiment_eligibility_projections` | Mutable (UPDATE) | Yes + FORCE | UPDATE allowed, DELETE rejected | After events | §4.2 |
| `contextual_verdict_projections` | Rebuildable | Yes + FORCE | Via SECURITY DEFINER function | After decisions | §4.2 |

### 4.2 New tables (addendum — normative in complete design candidate)

| Table | Type | RLS | Trigger | Erasure order | Addendum |
|---|---|---|---|---|---|
| `experiment_pattern_candidates` | Write-once | Yes + FORCE | Append-only | Erasure #1 (leaf) | §A7, §A10 |
| `experiment_next_round_records` | Write-once | Yes + FORCE | Append-only | Erasure #2 | §A6, §A10 |
| `experiment_notification_outbox` | Append-only (no delivery columns) | Yes + FORCE | Append-only | Erasure #3 | §A5, §A10 |
| `experiment_timeline_events` | Append-only | Yes + FORCE | Append-only | Erasure #4 | §A2, §A10 |
| `experiment_timeline_heads` | Mutable (head_sequence) | Yes + FORCE | Identity-immutable | Erasure #5 | §A2, §A10 |

### 4.3 Evolved tables

| Table | Changes | Migration step |
|---|---|---|
| `experiments` | `record_schema_version`, `started_at` columns + write-once triggers | §4.3 |
| `products` | `product_profile_entry_id` + partial unique + write-once trigger | §4.3 |
| `experiment_entries` | `workspace_id` + composite FKs | §4.4 |
| `experiment_ad_bindings` | `workspace_id`, `record_schema_version`, `experiment_version_candidate_id` + CHECKs | §4.4 |
| `judgements` | `workspace_id`, `record_schema_version`, `experiment_version_id` + CHECKs | §4.4 |
| `experiment_decisions` | `workspace_id`, `decision_schema_version` + CASE CHECK + new columns | §4.4 |
| `experiment_state_events` | `workspace_id` | §4.4 |
| `asset_qc_attempts` | `attempt_sequence`, `remediates_attempt_id`, `terminal_state` + triggers | §4.5 |
| `asset_versions` | `truth_dependency_*` four-field initialization + triggers | §4.6 |

---

## 5. Test ID registry — complete enumeration

### 5.1 Core test IDs (§5.2–§5.10)

| Prefix | Range | Count | Section |
|---|---|---|---|
| AC1 | 01–14 | 14 | §5.2 AC-1 |
| AC2 | 01–14 | 14 | §5.2 AC-2 |
| AC3 | 01–09 | 9 | §5.2 AC-3 |
| AC4 | 01–15 | 15 | §5.2 AC-4 |
| AC5 | 01–11 | 11 | §5.2 AC-5 |
| AC6 | 01–11 | 11 | §5.2 AC-6 |
| AC7 | 01–10 | 10 | §5.2 AC-7 |
| CE | 01–16 | 16 | §5.3.1 |
| CD-P | 1–4 | 4 | §5.3.2 |
| NL | 01–03 | 3 | §5.3.3 |
| HD | 01–09 | 9 | §5.3.4 |
| ECH | 01–07 | 7 | §5.3.5 |
| SI | 01–04 | 4 | §5.3.6 |
| LC | 01–17 | 17 | §5.4.1 |
| FG | 01–20 (incl. 12a) | 21 | §5.4.2 |
| EL | 01–12 | 12 | §5.4.3 |
| DP | 01–16 (incl. 06a, 06b) | 18 | §5.4.4 |
| IV | 01–10 | 10 | §5.4.5 |
| OC | 01–08 | 8 | §5.4.6 |
| SD | 01–07 | 7 | §5.4.7 |
| LO | 01–05 | 5 | §5.4.8 |
| IS | 01–05 | 5 | §5.4.9 |
| RC | 01–09 | 9 | §5.5.1 |
| QC | 01–23 | 23 | §5.5.2 |
| TD | 01–13 | 13 | §5.5.3 |
| CC | 01–13 | 13 | §5.5.4 |
| PFV | 01–05 | 5 | §5.5.5 |
| BE | 01–05 | 5 | §5.5.6 |
| FK | 01–10 | 10 | §5.6.1 |
| PU | 01–08 | 8 | §5.6.2 |
| AO | dynamic | dynamic | §5.6.3 |
| WO | 01–12 | 12 | §5.6.4 |
| RL | 01–15 | 15 | §5.6.5 |
| RG | 01–22 (incl. 15a, 15b, 20a) | 25 | §5.6.6 |
| SF | 01–15 (incl. 13a, 13b, 14a–14c) | 19 | §5.6.7 |
| ER | 01–07a | 8 | §5.6.8 |
| DC | 01–06, S1–S2 | 8 | §5.6.9 |
| TC | 01–05 | 5 | §5.6.10 |
| SQ | 01–05 | 5 | §5.7.1 |
| MU | 01–10 | 10 | §5.8.1 |
| MD | 01–11 | 11 | §5.8.2 |
| MC | 01–06 | 6 | §5.8.3 |
| AS | 01–12 | 12 | §5.9.2 |
| PG | 01–09 | 9 | §5.9.3 |
| RH | 01–04 | 4 | §5.9.4 |
| ET | 01–11 | 11 | §5.9.5 |
| PA | 01–12 | 12 | §5.9.6 |
| MP | 01–05 | 5 | §5.10.1 |
| PI | 01–07 | 7 | §5.10.2 |
| UB | 01–06 | 6 | §5.10.3 |
| SN | 01–10 | 10 | §5.11.7 |

### 5.2 Addendum test IDs (normative)

| Prefix | Range | Count | Addendum |
|---|---|---|---|
| AL | 01–14 | 14 | §A1 |
| TL | 01–15 | 15 | §A2 |
| NO | 01–18 | 18 | §A5 |
| NR | 01–27 | 27 | §A6 |
| PC | 01–22 | 22 | §A7 |
| ATI | 01–41 (07, 12, 13 retired) | 38 | §A10 |

---

## 6. Done gates

| Gate | Source | Blocking? | Status |
|---|---|---|---|
| `make verify` (SQLite + unit) | §5.11.1 | PR merge | Not yet run |
| `domain-rls` CI job (PostgreSQL) | §5.11.1 | PR merge | Not yet run |
| AC-1 through AC-7 tests pass | §5.2 | PR merge | Tests not yet implemented |
| §4.11 checks 1–88 mapped and pass | §5.13 | PR merge | All 88 mapped |
| All addendum tests pass | §5.14 | PR merge | Tests not yet implemented |
| Migration upgrades on both dialects | §5.8 | PR merge | Migration not yet implemented |
| Migration downgrades cleanly (empty schema-2) | §5.8.2 | PR merge | Not yet implemented |
| Security-negative tests pass on PG | §5.11.7 | PR merge | Not yet implemented |
| Structural bounds enforced | §5.11.5 | PR merge | Not yet implemented |
| No TODO/FIXME/HACK in touched files | §5.12 | PR merge | Not yet run |
| PR references Issue #22 | §5.11.9 | Issue close | No PR yet |
| Holistic final-design approval | — | implementation_ready | **APPROVED** 2026-09-03 — SHA-256 `1210427e` |

---

## 7. Design completeness and approval gate

`design_complete = true` — zero unresolved product-semantic decisions, zero known design gaps.

`implementation_ready = true` — holistic final-design approval granted 2026-09-03. Complete design SHA-256: `1210427eb9f6b479b84f167d435c7c354785c1f4c9b1d6a3b60f49245f902f8e` (5,471 lines).

---

## 8. Source manifest

| Component | File | SHA-256 | Precedence |
|---|---|---|---|
| Section 1 | `.claude/issue22-section1-final.md` | *computed at assembly* | Reconstructed; yields to approved §2–§4 on conflict |
| Section 2 | `.claude/issue22-section2-final.md` | `ba414f1b9e1e054128bbc37e1703d81463c9431532408167fc5fcbc9ad12c289` | **Approved — immutable** |
| Section 3 | `.claude/issue22-section3-final.md` | `c60217f93f6dc8220208f0b068827157bca00b4f81eadc62a4376584866008ba` | **Approved — immutable** |
| Section 4 | `.claude/issue22-section4-final.md` | `56c2b1ded7961d2682e8285042f9377e357d52974d96e06bec16176058f3bbf1` | **Approved — immutable** |
| Section 5 | `.claude/issue22-section5-draft.md` | *computed at assembly* | Normative (pending holistic approval) |
| Addendum | `.claude/issue22-cross-section-addendum-draft.md` | *computed at assembly* | Normative additions (pending holistic approval) |
| Delta | `.claude/issue22-approved-section-delta.md` | *computed at assembly* | Normative within design candidate; takes precedence where it explicitly names approved clause |
| Traceability | `.claude/issue22-final-acceptance-traceability.md` | *computed at assembly* | Derived from Sections 1–5, addendum, and delta |

**Precedence rules**: Approved Sections 2–4 are immutable source files with unchanged hashes. Section 1 yields to approved sections on conflict. The approved-section delta explicitly names modifications/extensions to approved clauses; it takes precedence within this design candidate only. Section 5 tests the contracts. The addendum defines normative additions.


---

## Source Manifest

| Source file | SHA-256 | Lines |
|---|---|---|
| `.claude/issue22-section1-final.md` | `a7e30277c1cf5859c71f98a3877fd8bd508ffaf73512f577208e10d9f5ac5288` | 428 |
| `.claude/issue22-section2-final.md` | `ba414f1b9e1e054128bbc37e1703d81463c9431532408167fc5fcbc9ad12c289` | 578 |
| `.claude/issue22-section3-final.md` | `c60217f93f6dc8220208f0b068827157bca00b4f81eadc62a4376584866008ba` | 621 |
| `.claude/issue22-section4-final.md` | `56c2b1ded7961d2682e8285042f9377e357d52974d96e06bec16176058f3bbf1` | 1139 |
| `.claude/issue22-approved-section-delta.md` | `3945341c3b59dab4947522c5295ea9515583bc2e2791a7c4ed2067bae5452a00` | 261 |
| `.claude/issue22-cross-section-addendum-draft.md` | `6374f23a0d4b1fca5f0ac5f0fbe4d7edd0df41c7613eaeecd22f4057752ba3dc` | 709 |
| `.claude/issue22-section5-draft.md` | `dc4c81c577b9ae14a2b174adea448441827a3bd8e3d34d728b04a3e7752ce46b` | 1396 |
| `.claude/issue22-final-acceptance-traceability.md` | `7373d01b4b5cba11952577035bf8169dfd3f4299ea2e69399babe42a2e025619` | 293 |
