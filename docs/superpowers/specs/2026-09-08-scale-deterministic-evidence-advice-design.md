# Deterministic Scale Evidence and Advice Design

Status: final master design

Date: 2026-09-08

Scope: v9 Scale Workspace area, Meta read-evidence consumption, deterministic
health/advice evaluation, Advice lifecycle, and Advice-to-Draft workflows.

## 1. Normative documents and precedence

The complete design is split into four implementation sections:

1. [Domain model and persistence](2026-09-08-scale-section-1-domain-model.md)
2. [Pure-function evaluator](2026-09-08-scale-section-2-pure-function-evaluator.md)
3. [Evidence, Advice, and orchestration](2026-09-08-scale-section-3-evidence-advice-orchestration.md)
4. [Domain Interface, Workspace UI, and validation](2026-09-08-scale-section-4-domain-interface-ui-validation.md)

The normative product behavior is the
[Scale advice semantics contract](../../product/scale-advice-semantics-contract.md).
Architectural constraints come from
[ADR 0008](../../adr/0008-separate-scale-evidence-advice-and-delivery-control.md)
and [ADR 0009](../../adr/0009-bind-meta-evidence-and-paused-launch-to-tenant-and-mode.md).

Precedence is: ADR and product contract, this master design, then the detailed
section responsible for the topic. A lower document may make a rule more exact
but may not weaken tenant isolation, evidence immutability, determinism,
privacy, or zero-Meta-write capability.

## 2. Product outcome

Scale answers one bounded question: for one confirmed contextual Winner bound
to one exact Meta delivery context, what does complete and comparable evidence
say about current health, and what should a human inspect or prepare next?

It delivers:

- explicit user-created monitoring Enrollments;
- immutable configuration-comparable Episodes;
- complete or visibly blocked read-evidence lineage;
- byte-replayable health and Advice candidate evaluation;
- auditable Advice threads that remain separate from user disposition;
- unpaid GenerationDraft and non-executable ScaleTestPlanDraft conversions;
- same-mode, evidence-backed, non-causal Outcomes;
- persisted API/UI state that survives browser, API, and Worker restart; and
- hard capability proof that Scale cannot mutate Meta.

It does not deliver automatic ad control, budget optimization, causal
attribution, demographic exclusions, customer real-Meta access, or a second
browser-owned business-state implementation.

## 3. Non-negotiable invariants

1. Scale owns evidence and advice, never Meta delivery control.
2. Analysis identity is contextual Winner plus exact remote binding, not global
   AssetVersion or ad name.
3. A Winner is only eligibility; Enrollment requires explicit user intent.
4. Material delivery changes split evidence into a new Episode.
5. Missing, incomplete, unreconciled, or privacy-suppressed evidence is never
   converted to zero or guessed.
6. Reach and frequency come from exact-window queries.
7. The evaluator is pure, deterministic, versioned, and has one external seam.
8. Advice is produced only by accepted immutable Evaluation evidence.
9. Advice lifecycle, user disposition, snooze, notification, conversion, and
   Outcome are orthogonal.
10. `customer_sandbox`, `controlled_mock`, and `internal_validation` remain
    isolated from source ingestion through Outcome.
11. Workspace/mode provenance is enforced by composite foreign keys and RLS,
    not only application filters.
12. Scale API and Worker cannot load, import, receive, dispatch, or persist a
    Meta write capability.

## 4. Canonical vocabulary

The three evidence modes are exactly:

- `customer_sandbox`;
- `controlled_mock`; and
- `internal_validation`.

There are no stored or wire aliases such as `sandbox`, `mock`, or
`real_read_only`. EvidenceMaturity uses `insufficient`; the phrase
"insufficient evidence" is display copy, not a second enum value.

The canonical domain terms are ScaleEnrollment, ScaleEpisode,
ScaleEvidenceSourceManifest, ScaleEvidenceSnapshot, ScaleEvaluation,
ScaleAdviceThread, AdviceOccurrence, AdviceDisposition, AdviceSnooze,
ScaleTestPlanDraft, AdviceOutcome, and UnobservedFactor. Specifications, schema,
tests, and UI code do not silently substitute synonyms.

## 5. Module map and seams

```text
#23 Meta read ingestion and reconciliation
  -> immutable Scale source revisions
  -> terminal ScaleEvidenceSourceManifest
       blocked  -> EvidenceReadinessProjection -> API/UI recovery state
       complete -> durable ScaleEvaluationRun
                     -> seal ScaleEvidenceSnapshot
                     -> evaluate_scale(snapshot)
                     -> immutable ScaleEvaluation
                     -> accepted Episode projection
                     -> Advice lifecycle / notification intents

Scale Advice occurrence
  -> Generation-owned create_draft_from_scale_advice(...)
  -> or Scale-owned non-executable ScaleTestPlanDraft

Owned-module subsequent evidence
  -> same-mode non-causal AdviceOutcome
```

The module interfaces are intentionally small:

```python
def evaluate_scale(snapshot: ScaleEvidenceSnapshot) -> ScaleEvaluation: ...

def request_scale_evaluation(
    session: Session,
    context: TenantContext,
    request: ScaleEvaluationRequest,
) -> str: ...

async def run_scale_evaluation(
    context: TenantContext,
    evaluation_run_id: str,
) -> ScaleRunOutcome: ...

def create_draft_from_scale_advice(
    session: Session,
    context: TenantContext,
    seed: ScaleAdviceDraftSeed,
) -> str: ...
```

Callers do not reproduce evaluator rules, evidence assembly, Generation draft
translation, or lifecycle transitions around these seams.

## 6. Enrollment and Episode lifecycle

Enrollment lifecycle is:

```text
draft -> active <-> paused -> closed
```

Activation repeats every eligibility check under lock, reserves the normalized
remote binding, and creates the first Episode from a fresh reconciled delivery
configuration source. Active and paused Enrollments reserve the binding; Draft
and closed Enrollments do not.

Episode rollover rules are deterministic:

- material configuration change closes the old Episode and opens a new
  collecting Episode when the Enrollment remains active and eligible;
- upstream withdrawal closes the Episode and Enrollment, supersedes Advice,
  fences work, and releases the binding; restored eligibility requires a new
  Draft Enrollment;
- explicit close closes the Episode and Enrollment and releases the binding;
- seven consecutive complete zero-delivery days close the Episode and move the
  Enrollment to paused while retaining the binding; eligible explicit resume
  creates a new Episode from fresh configuration.

Policy and Target versions are frozen per snapshot, not per Episode. Compatible
physical observation-contract generations may remain within an Episode but
advance data-continuity identity and apply Policy-defined streak resets.

## 7. Evidence states before and after sealing

Every requested Scale source revision ends in an immutable terminal manifest:

- `complete` proves all requested contracts, pages, and reconciliation are
  terminal and permits sealing; successful missing/suppressed values remain
  typed quality evidence; or
- `blocked` retains the failed contract/page/reconciliation lineage, typed
  issues, and recovery action but can never create a snapshot or Evaluation.

A blocked manifest advances a current readiness projection to maturity
`insufficient` and quality `blocked`. It preserves the last accepted Evaluation
and Advice as historical/stale. This satisfies operational visibility without
pretending incomplete transport evidence is a complete statistical input.

A complete manifest may seal exactly one canonical content identity for a
semantic claim. Snapshot parent, source links, and quality children are created
in one short transaction, become database-immutable at sealing, and include the
frozen predecessor streak input. The pure evaluator runs after that transaction
closes.

Privacy-suppressed breakdowns are a discriminated state with a typed reason and
no metric values. Database, assembler, response, and export layers all reject a
suppressed segment carrying metrics.

## 8. Deterministic evaluation

The evaluator accepts frozen values, performs no I/O or clock access, and
returns a value or a stable integrity error. It derives quality, maturity,
delivery state, primitive signals, streaks, health, breakdown findings,
scale-test eligibility, Advice candidates, and result hash in a fixed order.

Determinism includes:

- local Policy-defined Decimal context and canonical Decimal encoding;
- exact Wilson and Newcombe formulas and strict interval direction tests;
- SplitMix64 streams derived from snapshot hash and signal identity;
- fixed baseline-before-current draw order and rejection-sampled indices;
- nearest-rank, non-interpolated bootstrap percentiles;
- exact null-centering and inclusive tail comparison for breakdown p-values;
- deterministic Benjamini-Hochberg adjusted q-values; and
- canonical sort keys and separate hash domain separators.

New runs use the implementation's current algorithm version. The Scale runtime
retains a registry of every algorithm version referenced by a non-terminal run
or retained sealed snapshot. `evaluate_scale()` selects only the registered
implementation named by the snapshot's immutable Policy; HTTP callers cannot
select it. Removing an implementation requires an explicit retention/archive
migration and proof that no replayable record references it. A deployment may
therefore resume an older durable run without silently evaluating it under new
code.

## 9. Health and Advice

Health precedence and statistical thresholds are defined in Section 2. The key
product rule is conservative composition: frequency alone never means fatigue,
conflicting evidence never means healthy, and quality-blocked evidence never
creates deterministic Advice.

The five Advice types are:

- `refresh_creative`;
- `review_offer_or_landing_page`;
- `review_delivery_mix`;
- `investigate_delivery_anomaly`; and
- `consider_scale_test`.

An active thread is unique by Workspace, mode, Episode, type, and stable reason
signature. Occurrences append immutable evidence. Two eligible distinct-day
misses resolve a thread. Episode/semantic incompatibility supersedes it.
Freshness expiry expires it. Recurrence after resolved/expired creates a new
generation rather than reactivating history.

Dismissal and snooze suppress notification delivery according to frozen Policy
but do not erase occurrences or change system lifecycle. Conversion records user
intent and creates only a local Draft. Outcome requires later trusted evidence
and never asserts causation.

## 10. Transaction and recovery model

Evaluation orchestration uses three phases:

1. short transaction to lock, recheck, select predecessor, and seal/reuse the
   exact snapshot;
2. no open transaction while assembling immutable values and evaluating; and
3. short transaction to recheck eligibility, persist/reuse the Evaluation, win
   the structured projection CAS, and atomically apply Advice/outbox/audit.

The durable run is the checkpoint. Job payload contains only its ID. Lease
ownership, run revision, Episode cancellation generation, and projection
revision fence stale workers. A crash can repeat pure computation but cannot
duplicate immutable results, projections, occurrences, lifecycle events, or
notifications.

Historical corrections never directly regress current state. When history must
affect streaks, a new lineage generation replays local days in order; only its
final head may project and notify.

## 11. Domain Interface and Workspace UI

FastAPI exposes `/api/v1/scale/**` through a shallow Adapter. The only browser
path is the generated same-origin BFF at
`/api/domain/v1/w/{workspace_slug}/scale/**`. Every operation is represented in
OpenAPI, generated validators, operation registry, allowlist, paths, and
DomainGateway. Direct browser-to-FastAPI calls and hand-written Scale transports
are forbidden.

All commands use TenantContext, server authorization, RLS, idempotency,
revision preconditions, durable command-outcome recovery, and dual-actor audit.
The server returns `allowed_actions`; the browser does not reconstruct domain
eligibility.

The UI independently renders Enrollment lifecycle, Episode phase, Evidence
maturity, health, evidence freshness/quality, and Advice state. Blocked source
manifests remain inspectable with typed recovery actions. Historical evidence is
never silently presented as current. All evidence views and exports are
snapshot-stable, paginated where needed, and sanitized.

## 12. Security and zero-Meta-write proof

Scale runs in a dedicated capability-scoped composition root. It receives only
read-evidence adapters, no write token/environment, no MetaWritePort, no Launch
dispatch, and a database role without write-side grants.

The implementation gate combines:

- AST import restrictions;
- transitive import-closure proof;
- deployment environment and database-grant assertions;
- startup rejection of write capability;
- Worker job-dispatch isolation;
- OpenAPI absence of executable Meta mutation schemas/routes; and
- runtime spies across ingestion, evaluation, conversion, export, and recovery.

No individual test is sufficient by itself.

## 13. Implementation order

1. Add Section 1 enums, records, constraints, RLS, grants, and immutable event
   structures.
2. Implement Section 2 value contracts, canonical encoder, statistical helpers,
   evaluator registry, evaluator, and golden vectors.
3. Implement source-manifest/readiness ingestion, sealing, durable runs,
   orchestration, replay, Advice lifecycle, conversions, and recovery.
4. Implement Pydantic Domain Interface contracts and the shallow Scale router.
5. Regenerate the complete OpenAPI/BFF/Gateway seam atomically.
6. Build the persisted-state Pages Router UI and sanitized export path.
7. Pass pure, PostgreSQL/RLS, concurrency, recovery, capability, component,
   accessibility, controlled-Mock, and customer-Sandbox gates.
8. Replace the Scale placeholder in one atomic cutover; leave no fixture-backed
   or legacy path reachable.

## 14. Approval criteria

The design is ready for implementation planning only when:

- all five documents exist and cross-link correctly;
- canonical mode and maturity values match everywhere;
- blocked pre-snapshot evidence and privacy suppression have safe, representable
  states;
- every Enrollment/Episode terminal condition has a reachable next user action;
- statistical algorithms are specified precisely enough for independent golden
  implementations;
- durable runs can resume under their frozen algorithm version;
- database and RLS constraints carry tenant/mode invariants; and
- the validation matrix proves deterministic behavior and zero Meta writes.
