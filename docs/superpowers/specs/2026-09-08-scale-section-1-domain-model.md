# Design Section 1 (Final): Scale Domain Model and Persistence

Status: final design section

This section defines the Scale aggregate identities, immutable evidence model,
append-only records, mutable projections, PostgreSQL constraints, RLS scope, and
ownership seams. It is normative together with the
[Scale advice semantics contract](../../product/scale-advice-semantics-contract.md),
[ADR 0008](../../adr/0008-separate-scale-evidence-advice-and-delivery-control.md),
and [ADR 0009](../../adr/0009-bind-meta-evidence-and-paused-launch-to-tenant-and-mode.md).

Section 2 defines the pure evaluator over these values. Section 3 defines the
transactions and lifecycle that create them. Section 4 defines their public
projections. When this section conflicts with the product contract or an ADR,
the contract or ADR wins.

## 1.1 Aggregate and module shape

Scale is one domain module under `app/scale/`. It owns:

- `ScaleEnrollment` and `ScaleEpisode` lifecycle;
- Scale-specific immutable evidence sources and terminal source manifests;
- sealed `ScaleEvidenceSnapshot` and immutable `ScaleEvaluation`;
- `ScaleAdvice` threads, occurrences, system lifecycle events, user
  dispositions, snoozes, notification decisions, and conversion lineage;
- `ScaleTargetVersion`, test-plan drafts, unobserved factors, and sanitized
  export requests; and
- current read projections that always point back to immutable records.

It references but does not own Winner decisions, AssetVersion, ProductVersion,
OfferVersion, Landing Page, MarketAssetBinding, Reference rights, Generation
records, Experiments, Meta Connection, Meta call evidence, remote Meta objects,
or customer notification delivery.

The public evaluator interface is the deep seam:

```python
def evaluate_scale(snapshot: ScaleEvidenceSnapshot) -> ScaleEvaluation: ...
```

Persistence assembly, orchestration, HTTP transport, Meta ingestion, and
Generation conversion are adapters or neighboring modules. ORM records never
cross the evaluator interface.

## 1.2 Canonical values and orthogonal state

The following values are canonical in Python enums, PostgreSQL CHECK
constraints, OpenAPI, hashes, and browser responses. Aliases are rejected.

| Concept | Canonical values |
| --- | --- |
| MetaMode | `customer_sandbox`, `controlled_mock`, `internal_validation` |
| EnrollmentLifecycle | `draft`, `active`, `paused`, `closed` |
| EpisodePhase | `collecting`, `evaluating`, `closed` |
| EvidenceMaturity | `warming`, `monitorable`, `economics_ready`, `insufficient` |
| DataQualityState | `pass`, `warning`, `blocked` |
| ScaleHealth | `healthy`, `fatigue_watch`, `creative_fatigue`, `economics_decline`, `delivery_anomaly`, `not_delivering` |
| AdviceLifecycle | `active`, `resolved`, `superseded`, `expired` |
| AdviceDispositionKind | `acknowledged`, `dismissed`, `converted_to_draft` |
| SourceManifestState | `complete`, `blocked` |
| BreakdownEvidenceState | `observed`, `privacy_suppressed` |

Absence of a disposition means unreviewed. Health may be absent when evidence
is blocked, warming, or conflicting. These facts are not encoded by adding
values to another enum.

The three Meta modes come from #23 and are immutable provenance identities:

- `customer_sandbox` is the fixed customer Sandbox adapter;
- `controlled_mock` is the deterministic scenario adapter and requires a
  scenario version; and
- `internal_validation` is real read-only evidence restricted to the Platform
  Validation Workspace and its operator-authenticated identity seam.

`sandbox`, `mock`, and `real_read_only` are historical prose, not accepted
stored or wire values.

## 1.3 Tenant and provenance scope

Every tenant-owned table has `workspace_id NOT NULL`. Every evidence-bearing
table also has `meta_mode NOT NULL`. Records that depend on a Meta Connection
repeat and enforce this scope:

```text
(workspace_id, meta_mode, meta_connection_id, scenario_scope_key)
```

`scenario_scope_key` is a non-null generated or maintained key equal to the
controlled-Mock scenario version ID, otherwise the reserved `__no_scenario__`
marker. Real scenario IDs cannot use the reserved marker.

The connection invariant is:

```text
meta_mode = controlled_mock  <=> scenario_version_id IS NOT NULL
meta_mode != controlled_mock <=> scenario_version_id IS NULL
```

The #23 Meta Connection table exposes a supporting unique key over the complete
scope, including its equivalent scenario scope key. Composite foreign keys use
that key. Nullable `scenario_version_id` is never relied on for uniqueness,
deduplication, or mode isolation.

Meta ad-account identity, account timezone, and currency are repeated wherever
required for an enforceable composite relationship. Application-only filters
are insufficient. Customer delegated identities cannot read or create
`internal_validation` rows; those require the Platform Validation Workspace
and the separate operator-authenticated context required by ADR 0009.

## 1.4 ScaleEnrollment aggregate

`ScaleEnrollmentRecord` is the stable user intent to monitor one contextual
Winner against one normalized remote delivery binding. It contains:

```text
id
workspace_id
meta_mode
meta_connection_id
scenario_scope_key
ad_account_id
winner_decision_id
winner_confirmation_id
asset_version_id
product_version_id
offer_version_id
market_code
landing_page_version_id
market_asset_binding_id
campaign_remote_id
ad_set_remote_id
ad_remote_id
remote_binding_key
lifecycle
transition_reason
current_episode_id
creator_id
original_initiator_id
revision
evaluation_cancellation_generation
created_at
updated_at
closed_at
```

The normalized `remote_binding_key` is server-derived from the immutable
connection/account/campaign/ad-set/ad identity. The browser never supplies it.
The contextual upstream references are same-Workspace composite foreign keys;
mode-bearing references also require the same mode.

Lifecycle invariants are:

- `draft` has no binding reservation and no Episode;
- `active` reserves the binding and has one current open Episode;
- a user-paused Enrollment reserves the binding and may retain its open Episode;
- an Enrollment paused by `no_delivery_7_complete_days` reserves the binding,
  references its closed last Episode, and creates a new Episode only after an
  explicit eligible `resume`;
- `closed` is terminal, has no open Episode, and releases the binding.

A partial unique index over `(workspace_id, meta_mode, meta_connection_id,
ad_account_id, remote_binding_key)` where lifecycle is `active` or `paused`
enforces one reservation. Campaign/ad-set/ad nullability is normalized before
the key is calculated.

`ScaleEnrollmentEventRecord` is append-only and records every create, activate,
pause, resume, automatic no-delivery pause, close, and upstream-withdrawal
transition with old/new state, reason, semantic deduplication key, executor,
original initiator, and revision. The Enrollment row is a CAS-maintained
projection of that history.

Upstream withdrawal atomically closes the Enrollment and current Episode,
supersedes active Advice, advances the cancellation generation, and releases
the reservation. Restored eligibility never reopens them. A new qualifying
candidate creates a new Draft Enrollment.

## 1.5 ScaleEpisode aggregate

`ScaleEpisodeRecord` is one configuration-comparable observation interval. It
contains:

```text
id
workspace_id
meta_mode
enrollment_id
winner_decision_id / winner_confirmation_id
asset_version_id / product_version_id / offer_version_id
market_code / landing_page_version_id / market_asset_binding_id
meta_connection_id / scenario_scope_key / ad_account_id
campaign_remote_id / ad_set_remote_id / ad_remote_id
delivery_config_baseline_source_id
delivery_evidence_hash
delivery_comparability_hash
metric_semantic_contract_set_id
timezone_name
currency
attribution_semantics_hash
phase
started_at / started_local_day
ended_at / closure_reason
revision
projection_revision
projection_as_of_local_day
projection_source_revision
projection_semantic_event_sequence
projection_lineage_generation
accepted_evaluation_id
latest_source_manifest_id
phase_run_id
```

All context and provenance fields are immutable. Policy and Target are not
Episode fields because every snapshot freezes their applicable versions.
Physical #23 observation-contract generations may change only when the Scale
semantic contract declares them compatible; the snapshot freezes the actual
generations and advances data-continuity identity.

At most one non-closed Episode exists per Enrollment. The partial unique index
is backed by an Enrollment/Episode lock and CAS; the index is the final guard.

Episode phase is a recoverable projection:

- creation starts `collecting`;
- sealing a projection-eligible live run moves it to `evaluating` and stores
  `phase_run_id` in the same transaction;
- committing or terminalizing that run returns it to `collecting`, unless the
  Episode closes;
- retry resumes the same run, and stale workers cannot clear another run's
  phase because both run revision and `phase_run_id` are fenced;
- historical backfill and intermediate forward replay never change live phase.

`ScaleEpisodeEventRecord` is append-only. Closure reasons include configuration
sub-reasons, `no_delivery_7_complete_days`, `upstream_withdrawal`, and explicit
Enrollment close. Seven-day no-delivery closure also moves the Enrollment to
paused; configuration rollover opens a new Episode; upstream withdrawal closes
the Enrollment.

## 1.6 Policy, Target, and semantic contracts

`ScalePolicyVersionRecord` is immutable after publication. Every evaluator-used
choice is a typed non-null column or immutable typed child, never an opaque JSON
blob as the sole source of truth. It covers:

- compatible evaluator algorithm and canonical schema versions;
- Decimal precision/rounding and exact z value;
- evidence, activity, sample, spend, and freshness floors;
- signal thresholds and inclusive/strict comparison rules;
- streak carry/reset rules by typed reason;
- bootstrap PRNG, draw count, valid-draw ratio, nearest-rank percentiles, and
  draw order;
- breakdown families, sample/share gates, and BH FDR;
- advice severity, notification cooldown, and conversion-safe values; and
- mode permissions.

Published versions are never updated or deleted. A separate current-policy
projection selects the version for new runs. Existing snapshots retain their
version.

`ScaleTargetVersionRecord` belongs to one exact ProductVersion, OfferVersion,
market, and currency context. It contains optional target CPA, optional
break-even ROAS, required platform-valid minimum economic spend when needed,
and optional stricter thresholds. At least one economic target is required for
scale-test eligibility. Owner values may tighten but never weaken Policy.

Target values and applicability are immutable. A revisioned lifecycle
projection and append-only target events express publication/withdrawal. A
partial unique index permits one active Target per exact context. Publication
or withdrawal advances an ordered semantic-event sequence used by evaluation
projection ordering.

`ScaleMetricSemanticContractSetRecord` freezes the Scale meaning of daily,
exact-window, breakdown-family, and delivery contracts. Immutable child records
wrap exact #23 contract generations and hashes. Compatibility between physical
generations is explicit; absence of a declaration means incompatible.

## 1.7 Immutable source evidence and terminal manifests

Scale never seals from mutable "latest" projections. It retains immutable
source revisions linked to #23 SyncRun, occurrence, call intent, page attempts,
reconciliation, raw hashes, contract generation, account, timezone, currency,
mode, connection, and scenario scope.

Typed source tables are:

- `ScaleDailyMetricSourceRecord` with one child slot per requested complete
  local day;
- `ScaleExactWindowSourceRecord` for current 7d, baseline 7d, and rolling 14d;
- `ScaleBreakdownSourceRecord` plus observed daily children or a privacy
  suppression record;
- `ScaleDeliveryConfigSourceRecord` with full evidence and comparability hashes;
  and
- immutable source-to-call/page/reconciliation reference records.

Observed zero delivery is a present daily slot with zero primitives and
`delivered=false`. Missing evidence is not a row of zeros.

`ScaleEvidenceSourceManifestRecord` represents the terminal result for one
requested source revision. It contains:

```text
id / workspace_id / meta_mode / connection and scenario scope
episode_id
target_local_day
source_revision / source_generation
state                         complete | blocked
contract_request_set_hash
last_complete_local_day      nullable only when blocked before determination
safe_watermark
terminal_at
canonical_hash
```

Immutable manifest-item children enumerate every requested contract instance
and its terminal result. `ScaleSourceQualityIssueRecord` children use stable
code, severity, blocking flag, contract/window/segment locator, safe detail,
and recovery-action kind.

Minimum terminal-source codes are `transport_exhausted`,
`incomplete_pagination`, `reconciliation_unresolved`,
`contract_instance_failed`, `connection_ineligible`, and
`source_provenance_mismatch`. Minimum evaluator-visible snapshot codes are
`missing_daily_slot`, `exact_window_unavailable`, `breakdown_incomplete`,
`privacy_suppressed`, `stale_evidence`, `delivery_config_incompatible`, and
`contract_generation_warning`. Suppression reasons are a separate enum and
include at least `api_privacy_threshold` and `api_policy_omission`. An unknown
Provider string is retained only inside protected raw evidence and maps to a
stable contracted code; it never becomes a new enum at runtime. Adding or
reinterpreting a code requires a new Policy and, when hashing shape changes, a
new canonical schema version.

A `complete` manifest proves terminal reconciliation and a complete cursor/page
chain. Missing or privacy-suppressed values may still be complete when recorded
as typed quality. A `blocked` manifest records exhausted transport, incomplete
pagination, unresolved terminal reconciliation, contract mismatch, or other
source-integrity failure and can never be selected by a run.

`ScaleEvidenceReadinessProjection` is the current Episode read model ordered by
source revision. It points to the latest terminal manifest and exposes
maturity, quality state, freshness, safe issues, and recovery action. A blocked
manifest projects `maturity=insufficient`, `quality=blocked`, no current health,
and no new candidate while retaining the last accepted Evaluation as
historical/stale. This projection gives pre-snapshot failures an authoritative
UI representation without fabricating an Evaluation.

All source records, manifest parents/items, and source quality issues are
append-only and `ON DELETE RESTRICT` once referenced.

## 1.8 Sealed EvidenceSnapshot

`ScaleEvidenceSnapshotRecord` is created only from a `complete` source manifest.
Its parent freezes:

- Workspace, mode, connection, scenario, account, Episode, and binding;
- source manifest ID/hash/revision and watermarks;
- Policy, Target or normalized no-target key, algorithm, and canonical schema;
- Scale metric-contract set and wrapped #23 generations;
- timezone, currency, attribution, and delivery configuration;
- exact current/baseline windows and Episode complete/active-day counts;
- data-continuity generation;
- predecessor Evaluation and full prior streak state, or a typed absence reason;
- `last_sync_at`, frozen `freshness_reference_at`, and `as_of_local_day`; and
- `canonical_hash` and `sealed_at`.

Typed `ScaleEvidenceSourceRecord` children link every required source role.
`ScaleSnapshotQualityIssueRecord` children freeze evaluator-visible quality.
The snapshot does not copy a blocked source-manifest failure into invented
metric input.

Breakdown evidence is discriminated in persistence:

- `ScaleSnapshotBreakdownSegmentRecord.state='observed'` requires no suppression
  reason and owns complete daily children;
- `state='privacy_suppressed'` requires a typed suppression reason and database
  constraints prohibit metric children;
- transport DTOs and export assemblers enforce the same invariant.

Snapshot source links and quality children allow INSERT only while the parent
is unsealed, reject UPDATE/DELETE, and reject insertion after sealing. The parent
allows only one unsealed-to-sealed transition. The canonical payload has an
explicit schema and total sort keys; it does not reflect arbitrary ORM columns.

Uniqueness is enforced by both the semantic claim hash and scoped canonical
content identity described in Section 3. Nullable Target/scenario values are
normalized before any unique key.

## 1.9 Durable runs and immutable Evaluations

`ScaleEvaluationRunRecord` is a recoverable orchestration checkpoint. It freezes
the semantic request before work begins and stores run state, snapshot ID,
terminal Evaluation ID/outcome, lease/revision fence, trigger lineage,
cancellation generation, original initiator, and append-only run events.

The run uniqueness key includes Episode, target local day, trigger kind,
complete manifest hash/revision, freshness reference, Policy, normalized
Target/no-target, algorithm, semantic-event sequence, and replay generation.
Only the run ID enters the Worker job payload.

`ScaleEvaluationRecord` is immutable and contains the complete canonical result
payload, result hash, snapshot hash, algorithm, maturity, quality, health/reason,
and output streak compatibility. Typed immutable children retain signals,
Advice candidates, candidate evidence, streak counters, and reset records.

Its semantic fingerprint includes Workspace, mode, Episode, snapshot canonical
hash, metric-contract-set hash, Policy, normalized Target/no-target, and
algorithm. A duplicate must have byte-identical canonical result content or is
an integrity failure.

`ScaleEpisodeProjection` caches accepted maturity, health, streaks, and the
structured projection cursor. It is never source of truth and always references
the accepted immutable Evaluation. Historical, stale, and intermediate replay
Evaluations cannot update it.

## 1.10 Advice records

`ScaleAdviceThreadRecord` is the CAS projection for one active semantic identity:

```text
(workspace_id, meta_mode, episode_id, advice_type, reason_signature)
```

A partial unique index permits one active thread. Terminal threads are never
reactivated; recurrence creates a new generation with `predecessor_thread_id`.

Immutable records are:

- `ScaleAdviceOccurrenceRecord`, unique per Evaluation/type/reason signature;
- `ScaleAdviceThreadEventRecord` for created/resolved/superseded/expired;
- `ScaleAdviceDispositionRecord` for acknowledge/dismiss/convert;
- `ScaleAdviceSnoozeRecord` and cancellation/supersession linkage;
- `ScaleAdviceNotificationDecisionRecord` for send or suppression; and
- shared notification outbox records only for a send decision.

Lifecycle, disposition, snooze, and notification delivery are separate. User
roles cannot insert system lifecycle events. Every occurrence has a composite
foreign key proving its snapshot equals its Evaluation snapshot.

## 1.11 Conversion, outcomes, inspection, and export

`ScaleAdviceConversionRecord` is unique by Workspace, mode, and occurrence. It
freezes input lineage and has exactly one output:

- a Generation-owned unpaid `GenerationDraft`; or
- a Scale-owned non-executable `ScaleTestPlanDraftRecord`.

A CHECK and composite foreign keys enforce the exclusive output. Test-plan
generated criteria and lineage are immutable; only planned changes and
observation notes live in a revisioned editable projection.

`ScaleAdviceOutcomeRecord` is append-only and must reference trusted subsequent
owned-module evidence. Workspace and mode must match the Advice occurrence.
Disposition, notes, or screenshots cannot be its sole evidence.

`ScaleUnobservedFactorRecord` is append-only, revisioned by correction links,
and belongs to an Episode. It may reference safe uploaded objects through the
existing object boundary. It is never linked into an evaluator snapshot.

`ScaleExportRecord` references a durable Operation and immutable Enrollment,
Episode, and optional snapshot/evaluation validators. It stores only a safe
result reference after atomic object commit. Download intents use the existing
authorized object seam; raw payloads and object keys never enter Scale responses.

## 1.12 RLS, grants, and immutability

All Scale access runs in a tenant-scoped transaction. RLS policies compare
`workspace_id` to the transaction tenant context. Mode-aware policies also
enforce permitted modes for that context. Composite foreign keys remain
authoritative even if RLS or application code is misconfigured.

Database roles are capability-scoped:

- the Scale API role may use public command/query functions but cannot insert
  Evaluations or system lifecycle events directly;
- the dedicated Scale Worker role may ingest read evidence, seal snapshots,
  evaluate, and project, but has no Launch tables, Meta mutation job, or
  credential-secret grants;
- customer roles cannot access `internal_validation`; and
- no Scale role can update/delete immutable evidence, snapshots, Evaluations,
  occurrences, dispositions, outcomes, or events.

Triggers enforce immutable rows, legal parent/child sealing, mode/scenario
shape, protected breakdown exclusion, legal lifecycle transitions, and terminal
state. Historical references use `ON DELETE RESTRICT`.

## 1.13 Required constraint and model tests

The implementation must prove:

- all canonical enum values reject aliases;
- every cross-Workspace, cross-mode, cross-Connection, and cross-scenario
  relationship fails at the database layer;
- active/paused remote-binding reservation survives activation races;
- upstream withdrawal closes Enrollment/Episode and releases the reservation;
- no-delivery closure pauses Enrollment, retains the reservation, and resume
  creates a new Episode;
- only complete source manifests can seal snapshots;
- blocked manifests produce ordered readiness/quality projections but no
  snapshot or Evaluation;
- source and snapshot children become immutable at the specified transition;
- suppressed breakdown segments reject metric children at database, assembler,
  response, and export layers;
- nullable Target/scenario dimensions cannot weaken unique keys;
- same-day revisions, backfill, and replay cannot corrupt predecessor or live
  projection lineage;
- Advice state dimensions and one-active-thread constraints survive concurrency;
- one occurrence converts to exactly one output; and
- every immutable record remains readable and hash-verifiable after Policy,
  Target, connection, or current projection changes.

## 1.14 Explicit non-responsibilities

The domain model does not:

- store executable Meta budget, bid, targeting, creative, or status commands;
- treat current projections as immutable evidence;
- invent source metrics for failed ingestion or privacy suppression;
- let a browser choose Policy, Target, contract, algorithm, mode, account, or
  source manifest;
- treat user disposition or notes as causal outcome evidence;
- reopen a closed Enrollment/Episode automatically; or
- expose Meta write credentials, clients, jobs, tables, or grants to Scale.
