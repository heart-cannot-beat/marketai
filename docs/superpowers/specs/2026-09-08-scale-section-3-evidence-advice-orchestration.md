# Design Section 3 (Final): Evidence, Advice, and Orchestration

Status: final design section

This section defines everything around the pure Scale evaluator: immutable
evidence ingestion, snapshot sealing, durable evaluation orchestration, Advice
lifecycle, draft conversion, Enrollment and Episode lifecycle, scheduling,
recovery, upstream withdrawal, and zero-Meta-write capability separation.

The normative domain behavior remains in
[`docs/product/scale-advice-semantics-contract.md`](../../product/scale-advice-semantics-contract.md).
The aggregate and persistence invariants are defined in
[`2026-09-08-scale-section-1-domain-model.md`](2026-09-08-scale-section-1-domain-model.md).
The pure evaluator and its immutable value contracts are defined in
[`2026-09-08-scale-section-2-pure-function-evaluator.md`](2026-09-08-scale-section-2-pure-function-evaluator.md).
This section implements the separation required by ADR 0008 and the tenant and
mode provenance required by ADR 0009.

## 3.1 Module interfaces and durable run identity

Scale exposes one Worker-facing orchestration interface:

```python
async def run_scale_evaluation(
    context: TenantContext,
    evaluation_run_id: str,
) -> ScaleRunOutcome:
    """Resume one durable Scale evaluation run to a terminal outcome."""
```

`ScaleRunOutcome.kind` is one of:

- `evaluated`: a new Evaluation and its eligible projections were committed;
- `already_evaluated`: the immutable Evaluation fingerprint already existed and
  was reused; eligible projection work for this run was still reconciled
  idempotently;
- `episode_rolled_over`: configuration evidence closed the old Episode and
  opened a new collecting Episode, so the old Episode was not evaluated;
- `blocked`: upstream eligibility was withdrawn before evaluation;
- `cancelled`: pause, close, or another committed Episode transition made the
  run ineligible before result commit.

The outcome carries the applicable Enrollment and Episode IDs and an
`evaluation_id` only for the two evaluated outcomes. Configuration rollover and
upstream withdrawal are valid terminal outcomes, not exceptions disguised as
failed evaluations.

Schedulers do not construct a large call to this interface. They first create
or reuse a `ScaleEvaluationRunRecord` through an internal scheduling interface:

```python
def request_scale_evaluation(
    session: Session,
    context: TenantContext,
    request: ScaleEvaluationRequest,
) -> str:
    """Return the durable evaluation_run_id for one semantic request."""
```

`ScaleEvaluationRequest` freezes:

- Enrollment and Episode;
- target account-local day;
- trigger kind: `live`, `backfill`, `policy_change`, `target_change`, or
  `forward_replay`;
- the immutable reconciled `complete` source-manifest ID, hash, and source
  revision;
- an offset-aware `freshness_reference_at` derived from the persisted trigger
  event, never from a later retry's ambient clock;
- the scheduler-selected Policy and Target version or explicit no-target marker;
- the implementation algorithm version;
- replay generation and frozen base Evaluation when trigger kind is
  `forward_replay`;
- the applicable ordered semantic-event sequence for Policy/Target changes;
- triggering SyncRun, upstream event, and original initiator where applicable.

This is an internal interface. HTTP callers cannot choose a source manifest,
Policy, Target, algorithm label, replay generation, or semantic sequence. The
scheduler resolves them from persisted eligible domain state, and the algorithm
version for a new run always comes from the evaluator package's
`CURRENT_ALGORITHM_VERSION`.
Every optional identity dimension uses a canonical explicit marker or zero
generation in uniqueness keys; SQL `NULL` is never allowed to weaken replay
deduplication.

The run record is the recovery checkpoint. It records the selected
`snapshot_id`, optional terminal `evaluation_id`, terminal outcome, revision,
and append-only run events. A retry resumes this record; it never reinterprets
`episode_id` against whatever evidence happens to be newest at retry time.
Its projection moves by CAS through `requested -> snapshot_sealed -> evaluated`
or to terminal `episode_rolled_over`, `blocked`, `cancelled`, or
`failed_integrity`; retryable infrastructure failure remains in the Job state
and does not invent a terminal domain outcome.

`app/scale/evidence.py` owns two internal interfaces:

```python
def seal_snapshot(session: Session, context: TenantContext, run_id: str) -> str:
    """Seal or reuse the exact snapshot selected by the durable run."""

def assemble_snapshot(
    session: Session,
    context: TenantContext,
    snapshot_id: str,
) -> ScaleEvidenceSnapshot:
    """Load and verify one sealed snapshot as immutable value objects."""
```

The assembler returns a value. It does not call `evaluate_scale()`, persist an
Evaluation, choose a newer Policy or Target, or manage Advice. Orchestration is
the sole caller that crosses the evaluator interface.

## 3.2 Immutable evidence ingestion

### Source prerequisite

Section 3 requires a Scale-specific immutable evidence layer over the #23 Meta
observation contracts. Existing mutable/latest projections such as a daily row
updated by a later sync are not valid snapshot sources. Every source revision
must retain its exact observation-contract generation, SyncRun, ingestion,
pages or call evidence, reconciliation result, and raw hashes.

Source records and snapshot links carry an enforced non-null provenance scope
covering:

- Workspace;
- Meta evidence mode;
- Meta Connection and ad account;
- `controlled_mock` scenario version or an explicit no-scenario marker;
- observation-contract generation and contract hash;
- timezone and currency.

Composite foreign keys and mode-aware RLS enforce that scope. A nullable
scenario column is not relied on for isolation because SQL uniqueness and
foreign-key semantics around `NULL` would leave a provenance gap.

### Contract families

Scale uses four independent contract categories. The breakdown category has a
separate contract for each supplied family, so a complete Beta manifest
normally contains six contract instances: daily, exact-window, placement,
country, age/gender, and delivery configuration.

1. **Daily additive metrics.** One `DailyObservation` slot exists for every
   complete account-local day in the requested interval. A present slot stores
   ad-level spend, impressions, link clicks, purchases, and purchase value.
   An observed zero-delivery day remains present with zero primitive metrics
   and `delivered=False`. A missing slot has no metrics and requires a matching
   blocking typed `DataQualityItem`; it is never zero-filled.
2. **Exact-window reach and frequency.** Current 7-day, baseline 7-day, and
   rolling 14-day windows are three separately fingerprinted query instances
   under the exact-window contract. Their immutable results are never
   reconstructed from daily reach or frequency.
3. **Breakdowns.** Placement, country, and age/gender use independent contract
   instances and separate current-window queries. Each result preserves stable
   segment identity, daily primitive observations, privacy suppression, and a
   typed suppression reason. An omitted or suppressed segment is not invented
   as a zero-valued segment. Placement and country are separate statistical
   families; age/gender remains descriptive.
4. **Delivery configuration.** Campaign, ad-set, and ad configuration plus
   applicable status history and delivery diagnostics produce an immutable
   `MetaDeliveryConfigSnapshotRecord`.

Delivery configuration stores two different hashes:

- `evidence_hash` covers the full canonical contracted observation and proves
  source identity;
- `comparability_hash` covers only exact semantic fields whose change always
  breaks an Episode, excluding names, `updated_time`, and thresholded budget.

Budget is compared numerically with the Episode baseline under the frozen
delivery-contract rule. A categorical comparability-hash change or cumulative
budget change at or above the contract threshold produces a typed rollover
reason. A smaller budget edit changes the evidence hash but does not by itself
close the Episode.

### Terminal source manifests, readiness, and trigger

The #23 reconciliation path creates an immutable
`ScaleEvidenceSourceManifestRecord` whenever the complete requested contract
set reaches a terminal source outcome. Its state is discriminated:

- `complete`: every required query instance is terminal and reconciled, the
  final cursor/page chain is complete, and the manifest may be selected for
  snapshot sealing;
- `blocked`: transport exhaustion, terminal incomplete pagination, unresolved
  reconciliation, contract mismatch, or another source-integrity failure
  prevents snapshot sealing.

Both states enumerate the requested daily, exact-window, breakdown, and
delivery-configuration contract instances, retained call/page/reconciliation
evidence, last complete local day when known, source revision/generation, and
typed `ScaleSourceQualityIssueRecord` children. A successful response with
missing or privacy-suppressed values may still produce a `complete` manifest,
but it carries the corresponding typed quality inputs. A `blocked` manifest
never invents missing source rows, a data watermark, an EvidenceSnapshot, or an
Evaluation.

Every terminal manifest atomically advances an Episode-scoped
`ScaleEvidenceReadinessProjection` by source revision. For `blocked`, that
projection exposes EvidenceMaturity `insufficient`, DataQualityState `blocked`,
the exact source issues, safe provenance, and a typed recovery action such as
`retry_meta_sync`, `reconcile_meta_sync`, `repair_connection`, or
`wait_for_next_sync`. It retains the last accepted Evaluation and Advice as
historical/stale rather than presenting them as current. A retry creates a new
source revision and manifest; it never mutates the blocked manifest.

Live Scale scheduling is triggered only by creation of a `complete` reconciled
manifest after the account-local day completes. It is not triggered merely
because a wall-clock hour arrived. Policy/Target reevaluation and backfill use
the separate triggers defined in §3.8; they may reuse only a `complete`
immutable source manifest and always create a new semantic snapshot when
evaluator inputs change.

Each immutable source row is linked to a snapshot through a typed
`ScaleEvidenceSourceRecord`. Source rows pre-exist sealing; the snapshot parent,
source-link children, and `ScaleSnapshotQualityIssueRecord` children are created
together during sealing. Manifest quality issues and snapshot quality issues
share the same typed value contract but remain separate immutable records so a
pre-sealing failure is never disguised as evaluator input.

## 3.3 Snapshot sealing and assembly

### Transaction A: select, validate, and seal

Snapshot creation is one short tenant-scoped transaction:

1. Lock the `ScaleEvaluationRunRecord`, Enrollment, and current Episode. Verify
   their Workspace, mode, Connection, binding, state, and authorization epoch.
2. Recheck upstream eligibility. A withdrawal executes §3.9 and terminates the
   run as `blocked` without creating an Evaluation snapshot.
3. Load the run's immutable source manifest and require state `complete`; a
   `blocked` manifest cannot enter sealing. Compare delivery configuration with
   the Episode baseline. A contract-defined change atomically closes the old
   Episode, supersedes its active Advice, opens a new collecting Episode using
   the new baseline, records events and audit, and terminates the run as
   `episode_rolled_over`. Mixed pre/post-change windows are never evaluated.
4. Freeze the exact Policy, Target or no-target marker, metric-contract set,
   data-continuity generation, source manifest, and freshness reference selected
   by the run. Freshness quality is computed against that frozen reference.
5. Select the logical predecessor: the accepted projection-eligible Evaluation
   for the same Workspace, mode, Connection, and Episode with the greatest
   earlier account-local day on the selected lineage. The first item of a new
   forward-replay generation uses the run's frozen prior-generation base from
   the greatest earlier eligible day; later items use the preceding Evaluation
   in the new generation. A same-day revision never uses another same-day
   Evaluation as its predecessor. Any skipped local-day continuity is explicit
   in `data_continuity_generation` and follows the evaluator's reset rules.
6. Freeze the predecessor Evaluation ID and complete `PriorStreakState` before
   hashing. Absence is permitted only when no earlier distinct local-day
   Evaluation exists on the selected lineage, and freezes
   `prior_streak_absence_reason="no_earlier_distinct_day"`; an unexplained gap
   is an integrity failure. This also represents a legitimate same-day semantic
   reevaluation or replay of the Episode's first day, because another same-day
   Evaluation is deliberately ineligible. The snapshot builder never zeroes counters.
   Compatibility changes remain input for the evaluator, which applies
   Policy-defined resets and emits stable reset reasons.
7. Insert the unsealed snapshot parent, all source links, and all quality
   issues. Flush, construct the canonical payload, calculate its hash, and set
   `sealed_at` as the final mutation in the transaction.
8. Store `snapshot_id` on the run checkpoint and CAS the Episode phase to
   `evaluating` with `phase_run_id=run_id`, then commit. A historical backfill
   or intermediate replay does not change live Episode phase. A different live
   run cannot replace a non-terminal `phase_run_id`.

The canonical snapshot payload is explicitly defined rather than described as
"all database columns." It includes every evaluator-relevant value, source
identity/hash, frozen semantic version, data watermark, and prior streak input.
It excludes `snapshot_id`, `canonical_hash` itself, ORM-only surrogate IDs,
`created_at`, `sealed_at`, and other persistence metadata. `last_sync_at` and
`freshness_reference_at` remain included because they are explicit snapshot
values. Decimal values use normalized non-exponent strings. The domain separator is
`scale-evidence-snapshot-v1` and the canonical-schema version is part of the
payload.

Sources and issues use the exact total sort keys defined by the canonical
schema. Database uniqueness rejects duplicate semantic children; insertion
order can never affect the hash.

Two uniqueness layers prevent concurrent duplicate work:

- a snapshot claim hash covers Workspace, mode, Episode, local day, source
  manifest/revision, freshness reference, Policy, Target/no-target,
  semantic-event sequence, replay generation, predecessor, canonical schema,
  and algorithm version;
- `(workspace_id, meta_mode, canonical_schema_version, canonical_hash)` is
  unique for sealed snapshot content.

On a uniqueness race, the losing transaction rolls back its provisional rows
and loads the winning sealed snapshot.

### Database immutability

Database triggers and grants, not ORM convention, enforce:

- source evidence is append-only;
- source links and quality issues permit INSERT only while the parent is
  unsealed and reject UPDATE and DELETE at all times;
- the snapshot parent permits only the single unsealed-to-sealed transition;
- after sealing, parent INSERT-child, UPDATE, and DELETE operations fail;
- historical references use `ON DELETE RESTRICT`.

The parent is sealed only after every child is inserted, so the child gate does
not depend on deferred trigger ordering.

### Read-consistent assembly

`assemble_snapshot()` opens a new read-consistent tenant transaction after
Transaction A commits. It:

1. loads the sealed parent and every immutable child;
2. verifies Workspace, mode, Connection, provenance scope, scenario, Episode,
   Policy, Target, contract set, timezone, currency, source manifest, and all
   composite foreign-key relationships;
3. loads only the predecessor frozen by the snapshot and verifies its complete
   `PriorStreakState`; it never searches for a newer predecessor;
4. constructs frozen value objects in canonical order;
5. recomputes the canonical payload and compares the stored hash in constant
   time.

An unsealed snapshot, missing child, provenance mismatch, unexpected
predecessor, or hash mismatch raises a stable integrity error and does not call
the evaluator.

## 3.4 Evaluation orchestration and transaction boundaries

`run_scale_evaluation()` proceeds in three phases:

### Phase A: seal or reuse

Run Transaction A from §3.3. If the durable run already contains a sealed
snapshot, verify and reuse that exact ID. If it already has a terminal outcome,
return the recorded outcome without repeating side effects.

### Phase B: assemble and evaluate without an open transaction

Assemble the frozen snapshot in a short read transaction, close the transaction,
then call:

```python
evaluation = evaluate_scale(snapshot)
```

The deterministic bootstrap does not hold database locks or an RLS transaction
open. The Worker must preserve its lease through this phase using a heartbeat
mechanism that continues during CPU work, such as an executor/process or a
lease duration proven by the maximum-input benchmark. Completion is fenced by
the lease owner and run revision.

### Phase C: persist and project atomically

Open a second tenant transaction and lock the run and Episode. Recheck upstream
eligibility, Enrollment state, run cancellation generation, and whether the
Episode is still the run's open current Episode. A concurrent withdrawal
executes §3.9 and terminates the run as `blocked` without persisting the computed
result. A concurrent configuration rollover makes a live run
`episode_rolled_over`; pause, close, or another closure makes it `cancelled`.
Only an explicitly historical backfill run for an otherwise active, eligible
Enrollment may persist against an Episode previously closed by configuration
rollover, and it remains non-projecting. Then:

1. Insert or load `ScaleEvaluationRecord` by its full semantic fingerprint:
   Workspace, mode, Episode, snapshot canonical hash, metric-contract-set hash,
   Policy version, normalized Target/no-target key, and algorithm version.
   `snapshot_id` remains a foreign key but is not the only semantic identity.
2. Verify the persisted `result_hash` equals the evaluator result. A uniqueness
   conflict returns the existing byte-equivalent Evaluation; disagreement is an
   integrity failure. Reusing the immutable row does not automatically skip
   projection: a prior historical run may legitimately have persisted it
   without applying live state.
3. Decide whether this Evaluation may advance live projections. Eligibility is
   based on account-local day, source revision, semantic-event sequence, and
   replay lineage—not database insertion time or lexical Evaluation ID. An
   eligible result must CAS the Episode's integer `projection_revision` and
   replace its structured cursor `(as_of_local_day, source_revision,
   semantic_event_sequence, lineage_generation, evaluation_id)`. A historical
   backfill, stale semantic run, or intermediate forward-replay Evaluation is
   persisted but cannot mutate current Episode, streak, Advice, or notification
   projections.
4. For a projection-eligible result, persist maturity, health, and cached streak
   counters as replaceable UI projections referencing the immutable Evaluation.
5. Apply seven-consecutive-complete-no-delivery-day closure before ordinary
   Advice processing. If closing, record the Episode event, transition the
   Enrollment from `active` to `paused` with system reason
   `no_delivery_7_complete_days`, retain the binding reservation, fence new live
   evaluation/conversion work, supersede every active thread, and keep only
   `resume` and `close` available when their server checks permit them. Do not
   resolve threads by treating the closing Evaluation as an ordinary miss.
6. Otherwise process semantic supersession, Advice candidates, eligible misses,
   dispositions/cooldowns, and notification intents under §3.5.
7. Append audit events containing the system executor, original Enrollment
   creator/initiator, triggering SyncRun or semantic event, run ID, snapshot ID,
   Evaluation ID, and result hash.
8. Unless step 5 closed the Episode, CAS its phase back to `collecting` and
   clear `phase_run_id`, requiring it still names this run.
9. CAS the run to its terminal outcome and commit.

Evaluation, projection changes, Advice occurrences/events, notification outbox
rows, and audit events commit together. Job success is recorded afterward. If
the process dies after Phase C commits but before job success, retry returns the
run's terminal outcome and emits nothing twice.

## 3.5 Advice lifecycle

### Thread resolution and recurrence

Each `AdviceCandidate` resolves an active thread by:

```text
(workspace_id, meta_mode, episode_id, advice_type, reason_signature)
```

A partial unique index permits at most one active thread for that identity.
Thread creation and occurrence insertion run under the Episode lock; the unique
index is the final concurrency guard.

- An active match receives one immutable `AdviceOccurrenceRecord`.
- No active match creates a new thread generation, its `created` lifecycle
  event, and its first occurrence.
- A previously resolved or expired thread is not reactivated. Recurrence creates
  a new thread generation linked to the predecessor thread.
- A superseded thread is terminal for its old semantics.

An occurrence is unique by `(evaluation_id, advice_type, reason_signature)` in
the tenant/mode scope. Persistence derives `candidate_hash` from the complete
canonical candidate using the `scale-advice-candidate-v1` domain separator; it
does not ask the evaluator for a second decision field. The occurrence's
snapshot reference must equal the Evaluation's snapshot through a composite
foreign key.

### Eligible misses and transitions

Only accepted, projection-eligible, in-order live Evaluations on distinct
complete local days participate directly in thread miss counts. A final
forward-replay head may instead reconcile misses from the accepted replay chain
as a whole; intermediate replay rows never mutate the live counter one by one.
Warming, blocked-quality, historical backfill, stale lineage, Episode closure,
and same-day Policy/Target reevaluation do not accidentally count as a recovery
miss.

Transitions are:

| Condition | Transition |
| --- | --- |
| Candidate occurs | create active thread or append occurrence |
| Two eligible consecutive live days omit the active identity | `active -> resolved` |
| Episode closes | `active -> superseded` |
| Policy, Target, algorithm, or metric-contract identity changes | incompatible `active -> superseded` before new candidate resolution |
| Latest evidence passes its frozen freshness deadline | `active -> expired` |

Every transition, including creation, appends an immutable
`AdviceThreadEventRecord` with old/new state, trigger ID, actor kind, executor,
original initiator where applicable, and a semantic deduplication key. The
thread row is a CAS-maintained projection. There is no user-facing resolve
command. Database constraints restrict `resolved`, `superseded`, and `expired`
events to their allowed system trigger kinds; database grants prevent direct
user-role mutation.

Freshness expiry cannot depend on a later successful ingestion. Creating or
refreshing an active thread schedules a durable `scale_advice_expire` deadline
job keyed by thread and evidence deadline. The handler rechecks the latest
eligible evidence under lock and either expires the thread or exits because
newer evidence moved the deadline.

Every projection-eligible Evaluation advances the evidence-freshness cursor and
deadline for all active Episode threads, including a first eligible miss. This
does not create an occurrence; it prevents an old occurrence timestamp from
expiring a thread while newer recovery evidence is still fresh.

### Lifecycle, disposition, snooze, and notification remain separate

The four state dimensions are not collapsed:

- lifecycle: `active`, `resolved`, `superseded`, `expired`;
- disposition: absence means unreviewed; append-only records represent
  `acknowledged`, `dismissed`, and `converted_to_draft`;
- snooze: a separate append-only record stores actor, start, `snooze_until`,
  reason, and cancellation/supersession linkage;
- notification delivery: durable outbox states, separate from the thread and
  occurrence.

Disposition constraints require `linked_conversion_id` only for
`converted_to_draft`. Snooze fields never appear on a disposition record.
Disposition never changes lifecycle: dismissed Advice can resolve through
evidence, and snoozed Advice can be superseded by Episode closure.

Notification intent is created only for:

- the first occurrence of a new confirmed Advice thread; or
- a later occurrence whose severity exceeds the thread's previously notified
  severity.

`fatigue_watch` remains a health state and creates no AdviceCandidate, so it has
no Advice notification transition. A later confirmed fatigue candidate is the
first occurrence of its thread.

Dismiss cooldown and snooze suppress delivery, never occurrence creation.
Cooldown duration and severe-deterioration override are evaluated from the
applicable frozen Policy, and the computed suppression window/reason is stored.
Every notification or suppression decision has an immutable decision record.
Sendable notifications use the shared customer transactional-outbox
infrastructure through a general notification-intent interface, with a
deduplication key derived from the triggering occurrence and reason. The
existing guardrail-shaped writer must be generalized; Scale must not fill
guardrail-only columns with dummy values. Delivery adapters receive the same
idempotency key. Historical backfill and intermediate replay never enqueue
customer notifications.

## 3.6 Advice conversion

Conversion is a user command. It requires TenantContext, current authorization,
CAS revision, `Idempotency-Key`, and dual-actor audit. The command locks the
occurrence, thread, Episode, and relevant upstream versions and rechecks:

- the thread and occurrence are action-eligible;
- the Episode and Enrollment have not closed or become blocked;
- Approved, Winner confirmation, Offer, Truth, binding, and Reference rights
  remain valid;
- Workspace and Meta mode match the conversion target;
- the requested conversion type matches the Advice type.

The checks and created records commit in one tenant transaction.
Successful conversion also appends the `converted_to_draft` disposition linked
to the immutable conversion record in that transaction.

### `refresh_creative` to GenerationDraft

Scale calls a Generation-owned in-process interface rather than constructing a
Generation ORM record directly:

```python
def create_draft_from_scale_advice(
    session: Session,
    context: TenantContext,
    seed: ScaleAdviceDraftSeed,
) -> str:
    """Create an unpaid editing Draft with frozen Scale lineage."""
```

`ScaleAdviceDraftSeed` is declared in the Generation contracts package and
contains only immutable scalar/version references. The Generation module does
not import `app.scale` or its ORM models; Scale maps its records into this
interface value before crossing the seam.

`ScaleAdviceDraftSeed` freezes:

- the acted-on occurrence and its evidence snapshot;
- Winner decision and confirmation;
- ProductVersion, OfferVersion, market, Landing Page, and source AssetVersion;
- winning DimensionConceptKey plus Hook/Angle defaults;
- must-match and safety-rule source versions;
- original GenerationRecipeSnapshot ID and lineage hash;
- an explicit refresh axis selected by the user where required;
- the allowed mapping from Meta evidence mode to Generation execution mode.

The Generation module owns translation into its editable draft interface,
including provider/model defaults from the original recipe. It must not resolve
the current Product version in place of the frozen ProductVersion.
`regeneration_intent="new_generation"` creates state `editing`; no
ResolutionPreview, cost Reservation, Provider call, Run, or AssetVersion is
created.

`ScaleAdviceConversionRecord` preserves the full seed lineage, an output kind,
and exactly one of `generation_draft_id` or `scale_test_plan_draft_id`, enforced
by a CHECK plus composite foreign keys. `(workspace_id, meta_mode,
occurrence_id)` is unique. Concurrent duplicate conversion rolls back the
losing provisional Draft and returns the winner. Reusing an HTTP idempotency
key for a different request is an error; using a new key for the same occurrence
still returns the existing Draft.

The user must subsequently complete ResolutionPreview, cost confirmation,
submission, QC, and a new Experiment. The Draft cannot replace a real Meta ad
or inherit the Winner decision.

### `consider_scale_test` to ScaleTestPlanDraft

Conversion creates one idempotent `ScaleTestPlanDraftRecord` per occurrence.
Its immutable generated section contains:

- the exact baseline metrics and Evaluation references;
- success and stop criteria derived from the frozen Target;
- risk and blind-spot summary from typed signals;
- Policy-versioned manual Ads Manager checklist;
- Policy-versioned observation-period suggestion.

Only `planned_changes` and `observation_notes` are user-editable, using CAS
revision. Generated lineage, criteria, and risk are never overwritten by an
edit. Planned changes are notes/checklist data, not a machine-executable Meta
command, and the system does not generate a generic budget multiplier.

The Draft has no adapter or route capable of applying changes. Later remote
change is proven only by a new delivery configuration observation and Episode
rollover.

## 3.7 Enrollment and Episode lifecycle

### Enrollment commands

Enrollment state is:

```text
draft -> active <-> paused -> closed
```

`closed` is terminal. Public commands are:

- `create_draft_enrollment()`;
- `activate_enrollment()`;
- `pause_enrollment()`;
- `resume_enrollment()`;
- `close_enrollment()`.

Every command requires TenantContext, current authorization, expected revision,
`Idempotency-Key`, and dual-actor audit.

Draft creation validates the selected contextual Winner, Approved status,
Offer/Landing Page/market/binding consistency, mode, Connection access, rights,
and remote binding shape. Activation repeats every gate under lock because
eligibility may have changed while the Draft was reviewed. It also requires a
fresh reconciled delivery-configuration source and creates the first Episode
with that exact baseline in the same transaction.

A PostgreSQL partial unique index reserves a normalized remote binding key for
both `active` and `paused` Enrollments in the same Workspace, mode, Connection,
and ad account. Draft Enrollments do not reserve it. Nullable campaign/ad-set
fields are normalized before uniqueness so `NULL` cannot defeat the constraint.

Pause stops new evaluation requests, advances an evaluation-cancellation
generation that fences pending/in-flight live runs, and retains the binding and
current Episode. Account evidence ingestion may continue. Resume repeats
upstream and configuration checks: stable configuration and continuous evidence
can continue an open Episode; a previously closed no-delivery Episode always
causes resume to create a new Episode from fresh reconciled configuration. A
contract-defined change rolls over an otherwise open Episode before evaluation.
Close also fences pending/in-flight runs, closes the current Episode as
superseded, and releases the binding reservation.

### Episode invariants

Episode creation freezes all comparability inputs:

- Winner decision and confirmation;
- AssetVersion, ProductVersion, OfferVersion, market, Landing Page, and binding;
- Meta scope and evidence mode;
- delivery baseline and delivery-contract version;
- Scale metric semantic-contract set, timezone, currency, and attribution
  semantics.

The Episode does not freeze one physical #23 observation-contract generation.
Each snapshot freezes the actual wrapped generations it used. A newly published
generation may remain in the Episode only when its declared Scale semantics are
compatible; it advances `data_continuity_generation` and applies the Policy's
contract-change streak resets. An incompatible metric meaning, attribution,
timezone, currency, account, or evidence mode closes the Episode rather than
mixing evidence.

Policy and Target are not frozen on the Episode. The exact applicable versions
are frozen independently in every EvidenceSnapshot, so semantic reevaluation
does not rewrite the Episode.

Episode phase is `collecting`, `evaluating`, or `closed`. The immutable
Evaluation remains source of truth; phase, current maturity/health, and streak
counters are replaceable projections with an Evaluation cursor.

Every phase transition and closure appends a `ScaleEpisodeEventRecord`.
Automatic closure reasons are:

- `delivery_configuration_changed` with typed sub-reasons;
- `no_delivery_7_complete_days`;
- `upstream_withdrawal`.

Configuration change and withdrawal follow §§3.3 and 3.9. Seven-day
no-delivery closure uses distinct complete local days from the evaluator's
stable signal; same-day reevaluation cannot advance the count twice.

All evaluation, rollover, pause/resume, and withdrawal paths serialize on the
Enrollment/Episode lock and finish with CAS. Database uniqueness is the final
guard against two current Episodes.

## 3.8 Scheduling, backfill, semantic reevaluation, and recovery

### Trigger sources

`scale_evaluate` jobs are requested by four sources:

1. a reconciled `complete` source manifest for a complete account-local day of
   a live active Enrollment;
2. a `complete` Meta correction/backfill manifest;
3. publication or withdrawal of an applicable Policy or Target, reusing only
   source evidence that still satisfies freshness and contract requirements;
4. deterministic forward replay after an accepted historical correction.

The job payload contains only `evaluation_run_id`; the durable run contains the
semantic request. The job idempotency key is:

```text
scale-eval:{evaluation_run_id}:{algorithm_version}
```

The Worker calls the evaluator registry defined in Section 2. A deployment may
change `CURRENT_ALGORITHM_VERSION` for new requests, but a retry continues to
use the run's frozen registered implementation. Deployment validation fails if
it would remove an algorithm still referenced by a non-terminal run or a
replayable retained snapshot; silently substituting the new algorithm is never
allowed.

The run's semantic uniqueness includes Episode, target local day, trigger kind,
source-manifest hash/revision, freshness reference, Policy, Target/no-target,
algorithm, semantic-event sequence, and replay generation. A corrected manifest
therefore creates a new run and snapshot for the same local day, while
redelivery of the same event reuses the old run.

Policy/Target events first supersede incompatible active Advice atomically with
the semantic event. If fresh source evidence exists they request reevaluation;
otherwise the Enrollment returns to collecting/stale UI state until a complete
manifest arrives. Old Evaluations are never reinterpreted.

### Backfill lineage

Every backfill Evaluation is immutable and labeled with its trigger kind. A
historical backfill older than the live projection cursor cannot directly
change current streaks, Episode state, Advice, or notifications.

If corrected history must affect the current streak, orchestration creates a
new lineage generation and replays each affected complete local day in order.
Each replay snapshot freezes the preceding replay Evaluation. Intermediate
replay results remain non-projecting and non-notifying; only the final head may
win the monotonic projection CAS. At that point the Advice reconciler derives
current occurrences and eligible misses from the accepted replay chain, rather
than replaying historical side effects one day at a time. Historical
notifications are never sent; a newly current, still-fresh Advice may create one
current notification decision under the normal cooldown rules.

A backfill at or beyond the current cursor may update live state only if it wins
the projection-revision CAS, is newer under the structured cursor rules, and has
no skipped intervening complete day. The phrase "more recent" always refers to
this logical cursor, not database insertion time or Evaluation ID ordering.

### Lease and crash recovery

The existing persistent Job lease owns dispatch; the Scale run owns domain
progress. Scale jobs are safe to retry because they make no remote paid or Meta
write call.

- Crash before snapshot commit: retry repeats Transaction A.
- Crash after snapshot commit: run checkpoint reloads the exact sealed snapshot.
- Crash after evaluation computation: deterministic computation may repeat.
- Crash after Evaluation/Advice commit: terminal run outcome is returned; unique
  fingerprints, occurrence keys, lifecycle-event keys, and outbox keys prevent
  duplication.
- Lease loss before Phase C commit: the stale owner fails the lease/revision
  fence and cannot project results.
- A deterministic integrity failure terminalizes the run in a short fenced
  transaction, returns an Episode whose `phase_run_id` still names that run to
  `collecting`, and records a safe operator-visible integrity incident without
  fabricating DataQuality evidence or an Evaluation. A retryable infrastructure
  failure does not clear the phase because the same durable run must resume.

## 3.9 Upstream withdrawal

Withdrawal of Approved status, Winner confirmation, Offer, Truth,
MarketAssetBinding, Reference rights, Workspace qualification, or Connection
eligibility produces a durable upstream event with a unique semantic event ID.
The owning module writes that event/outbox in the same transaction as the
withdrawal. A Scale inbox or same-transaction in-process handler consumes it
idempotently; an unreliable best-effort callback is not sufficient.

Under the Enrollment lock, withdrawal handling:

1. transitions an `active` or `paused` Enrollment to terminal `closed` with
   reason `upstream_withdrawal`, advances its cancellation generation, and
   rejects new evaluation requests;
2. closes the current Episode with `upstream_withdrawal` when one exists;
3. releases the normalized remote-binding reservation in the same transaction;
4. supersedes active Advice and blocks new conversions;
5. appends Enrollment lifecycle, Episode, Advice, and audit events;
6. enqueues one deduplicated high-priority customer notification;
7. preserves all source evidence, source manifests, snapshots, Evaluations,
   Advice, dispositions, conversions, and audit history.

Every evaluation and conversion transaction repeats the upstream check, closing
the race between event delivery and a concurrent action. Restoring eligibility
does not silently reopen a closed Enrollment, Episode, or withdrawn version. A
newly eligible candidate may create a new Draft Enrollment and, after explicit
activation, reserve the now-released binding and create a new Episode.

## 3.10 Zero Meta write enforcement

### Capability isolation

Scale runs in a dedicated composition root such as `ScaleWorkerRuntime`, or an
equivalently capability-scoped Worker deployment. That runtime:

- dispatches Scale and read-evidence jobs only;
- is constructed only with the read-only Meta evidence port and its
  `internal_validation`, `customer_sandbox`, or `controlled_mock` adapters;
- does not import the Launch runtime, `MetaWritePort`, or write adapter;
- receives no Meta write token or write-enabled environment variable;
- uses a database role without Launch mutation or credential-secret grants.

Adding a Scale handler to a shared runtime that imports or retains a Meta write
adapter does not satisfy this requirement, even when the handler never calls
that attribute in tests.

### Static enforcement

- AST scanning rejects imports from `app.adapters.meta.write_client`,
  `app.launch`, and any module that exposes Meta mutation capability anywhere
  in `app/scale/`.
- An import-closure test starts from the Scale composition root and proves that
  neither the write adapter nor `META_WRITE_ACCESS_TOKEN` /
  `META_WRITE_ENABLED` is reachable transitively.
- Shared configuration must be split as needed so importing tenant persistence
  does not eagerly read Meta write settings in the Scale process.
- Scale's `MetaEvidencePort` contains read methods only and has
  `internal_validation`, `customer_sandbox`, `controlled_mock`, and test
  adapters at the same seam.
- Deployment tests prove the Scale process environment and database grants do
  not contain write capability.

### Runtime enforcement

- A spy instruments every Meta write adapter while the complete Enrollment,
  ingestion, evaluation, Advice, conversion, rollover, and recovery paths run;
  the call count remains zero.
- Scale startup fails if a write credential or write-capable adapter is
  injected.
- Worker dispatch tests prove Scale cannot claim a Launch mutation job.

### Route and architecture enforcement

Scale routes expose no command that maps to budget, bid, targeting, delivery
status, campaign, ad-set, or ad mutation. User-authored test-plan notes may
describe intended manual changes, but they are stored as non-executable text or
checklist data and never become an adapter request. OpenAPI contract tests prove
there is no executable Meta mutation schema or proxy route.

Scale may read delivery evidence and create MarketAI-local Drafts. It never
pauses, enables, deletes, retargets, rebids, or changes the budget of a Meta
object.

## 3.11 Required tests

### Evidence and sealing

- all contract families, pagination, raw hashes, reconciliation, provenance,
  mode, scenario, timezone, currency, and watermarks must match;
- terminal transport, pagination, reconciliation, and contract failures create
  one immutable blocked source manifest plus an insufficient/readiness
  projection and recovery action, but no snapshot or Evaluation;
- observed zero delivery remains distinct from a missing day;
- exact-window metrics are independent of daily rows;
- privacy-suppressed breakdowns retain no metric values and cannot serialize or
  export protected evidence;
- irrelevant config metadata and sub-threshold budget edits do not roll over an
  Episode, while every contract-defined semantic change does;
- parent/child insertion order cannot change the snapshot hash;
- unsealed, tampered, cross-tenant/mode, missing-child, updated, or deleted
  snapshots/source rows fail at the database and assembler layers;
- concurrent sealers converge on one content-identical snapshot.

### Orchestration and recovery

- no evaluator transaction remains open during deterministic computation;
- full Evaluation fingerprints handle explicit no-target without nullable-unique
  loopholes;
- an in-flight durable run survives deployment of a new current algorithm by
  resuming through its frozen registered implementation;
- every crash point resumes without duplicate snapshot, Evaluation, projection,
  occurrence, event, audit, or notification;
- stale lease owners and stale CAS revisions cannot project;
- configuration rollover and withdrawal terminate without evaluating mixed or
  invalid evidence;
- seven distinct complete no-delivery days close once, move the Enrollment to
  paused, retain its binding, and require explicit resume into a new Episode.

### Advice and conversion

- first occurrence, severity increase, cooldown, severe override, snooze,
  suppression, delivery, expiry, resolution, recurrence, and supersession are
  independently tested;
- watch health creates no thread or notification;
- blocked/warming/backfill/same-day semantic evaluations do not count as misses;
- users cannot create system lifecycle events or resolve threads;
- conversion concurrency returns one GenerationDraft or TestPlanDraft;
- every lineage reference and upstream gate is rechecked atomically;
- Generation conversion creates no Reservation, Provider call, Run, Asset, or
  Meta call;
- generated test-plan criteria remain immutable while editable notes use CAS.

### Enrollment, replay, and security

- active and paused binding uniqueness survives concurrent activation;
- activation/resume recheck eligibility and configuration under lock;
- historical backfill cannot regress the live cursor or notify;
- forward replay reconstructs prior streak lineage in local-day order and only
  its head may project;
- durable upstream events close the Enrollment/Episode, release the binding,
  and block exactly once; a newly eligible candidate can subsequently reserve
  that binding through a new Enrollment;
- AST, import-closure, environment, database-role, Worker-dispatch, OpenAPI, and
  runtime-spy tests jointly prove zero Meta write capability.

## 3.12 Explicit non-responsibilities

These modules do not:

- reinterpret or mutate an immutable Meta source revision;
- interpolate missing evidence or reconstruct non-additive metrics;
- keep a database transaction open while evaluating;
- let insertion time decide streak chronology;
- treat disposition, snooze, notification, or Advice lifecycle as one state;
- infer that a user carried out a remote change from a note or disposition;
- create paid Generation work during conversion;
- reopen a closed Episode or withdrawn upstream version automatically;
- claim causal attribution from Advice outcomes;
- load, possess, proxy, or call any Meta write capability.
