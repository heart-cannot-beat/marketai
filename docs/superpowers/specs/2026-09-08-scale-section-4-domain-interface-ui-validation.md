# Design Section 4 (Final): Domain Interface, Workspace UI, and Validation

Status: final design section

This section defines the public Scale Domain Interface, its generated
browser-to-domain seam, the persisted-state Workspace UI, and the automated
evidence required to ship the feature. It does not redefine evaluation or
orchestration semantics.

The normative domain behavior remains in
[`docs/product/scale-advice-semantics-contract.md`](../../product/scale-advice-semantics-contract.md).
The aggregate and persistence invariants are defined in
[`2026-09-08-scale-section-1-domain-model.md`](2026-09-08-scale-section-1-domain-model.md).
The pure evaluator is defined in
[`2026-09-08-scale-section-2-pure-function-evaluator.md`](2026-09-08-scale-section-2-pure-function-evaluator.md),
and evidence, Advice, conversion, lifecycle, scheduling, and recovery are
defined in
[`2026-09-08-scale-section-3-evidence-advice-orchestration.md`](2026-09-08-scale-section-3-evidence-advice-orchestration.md).

When this section conflicts with an ADR or the product contract, the ADR and
product contract win. Browser convenience never weakens tenant, mode,
provenance, revision, or zero-Meta-write boundaries.

## 4.1 Module and transport boundaries

### FastAPI Domain Interface

Scale exposes a shallow HTTP Adapter in `app/scale/v1_router.py`:

```python
router = APIRouter(prefix="/scale", tags=["scale"])
```

`app.main.v1_router`, whose prefix is `/api/v1`, includes this router. The
resulting FastAPI surface is `/api/v1/scale/**`. Scale does not add a second
unversioned router and does not put browser-facing Workspace slugs into the
FastAPI path.

Every handler:

- declares a stable `operation_id`, request model, response model, response
  size policy, and success status;
- obtains `TenantContext` with `Depends(request_tenant_context)`;
- delegates to an application/domain interface and performs no direct ORM
  orchestration in the route;
- executes tenant persistence through the RLS-scoped session boundary;
- maps domain failures to `application/problem+json` with stable error codes;
- emits both executor and original initiator in mutation audit records; and
- returns server-computed `allowed_actions`; the browser never reconstructs
  lifecycle or permission rules.

The HTTP Adapter may import Scale contracts and application services. It may
not import the evaluator implementation, a Meta write port, Launch dispatch,
or mutable state owned by another module.

### Generated BFF seam

The only browser-to-domain path is:

```text
/api/domain/v1/w/{workspace_slug}/scale/**
```

The existing same-origin BFF resolves the slug, validates the active session,
host, origin, CSRF token, path, query, body, response, and deadlines, then
issues delegated identity to FastAPI. The slug is routing context, never tenant
authorization.

Scale operations must be added to the generated OpenAPI chain:

```text
FastAPI response/request models
  -> contracts/openapi/marketai-domain-v1.json
  -> generated validators, operation registry, paths, and allowlist
  -> DomainGateway operations
  -> Scale browser hooks and components
```

No Scale page uses direct `fetch()` to FastAPI, a hand-written transport, a
legacy API path, a Server Action, or a browser fixture as domain truth.

### Pages Router UI

The repository uses Next.js Pages Router. Public pages are therefore:

```text
web/pages/w/[slug]/scale.tsx
web/pages/w/[slug]/scale/[enrollmentId].tsx
web/pages/w/[slug]/scale/[enrollmentId]/breakdowns.tsx
web/pages/w/[slug]/scale/[enrollmentId]/evidence.tsx
web/pages/w/[slug]/scale/targets.tsx
```

Reusable feature code lives under `web/features/scale/`. There is no
`web/src/app/(workspace)` Scale tree and no App Router migration hidden inside
this feature.

## 4.2 Shared HTTP contract

### Identity, authorization, and RLS

FastAPI derives Workspace, role, actor, authorization epoch, and mode access
from delegated identity and server-owned records. Request bodies cannot supply
`workspace_id`, actor identity, role, evidence mode, account identity, source
manifest, Policy selection, Target selection, algorithm version, or adapter
kind.

The only persisted and wire-level evidence-mode values are
`customer_sandbox`, `controlled_mock`, and `internal_validation`, matching the
#23 Meta connection contract. Historical prose such as `sandbox`, `mock`, or
`real_read_only` is never accepted as an alias.

Owner and Member permissions follow the product contract. Owner-only Target
commands fail server-side even when a browser fabricates an enabled control.
Every object lookup is scoped by Workspace and, where applicable, evidence
mode. Composite foreign keys and RLS reject mixed Workspace or mode lineage.

### Idempotency and recoverable browser intent

Every non-safe Scale operation declares `x-marketai-idempotent: true` and
requires an `Idempotency-Key` header at FastAPI. The request body never contains
`idempotency_key`.

The browser gives the generated `DomainGateway` a stable action-instance ID.
The existing `IntentTracker` persists the unresolved intent and sends
`X-Intent-Id` to the BFF. After authentication and route validation, the BFF:

1. normalizes the browser intent;
2. derives `Idempotency-Key` from operation ID and normalized intent;
3. forwards the normalized intent as `X-Command-Identity` for Scale commands;
4. never logs either raw header; and
5. retains ambiguous intent until authoritative reconciliation.

Scale stores only the command-identity hash. A success or replayable terminal
Problem outcome is persisted with the idempotency record. The read-only
recovery operation is:

| Method | FastAPI path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/scale/command-outcomes/{command_identity}` | Recover the authoritative result of one actor-scoped Scale command after refresh, timeout, or lost response |

The query may include a bounded `command_kind` discriminator. Reconciliation
never repeats the domain mutation. Actor, Workspace, operation kind, request
fingerprint, method, path, query, and precondition participate in the stored
identity/fingerprint contract.

### Optimistic concurrency

Mutable Scale resources expose a monotonically increasing integer `revision`
and `ETag: "<revision>"`. Mutating operations carry the current validator in
`If-Match`; the request body never contains `revision` or
`expected_revision`.

Responses are:

- `428 precondition_required` when a required `If-Match` is absent;
- `400 invalid_precondition` when it is malformed;
- `409 revision_conflict` with the current revision when it is stale.

Creation commands also bind an authoritative source projection:

- Enrollment creation uses the candidate projection revision;
- Target publication preview uses the applicable Target-context revision;
- UnobservedFactor creation uses the Episode inspection revision;
- conversion uses the AdviceThread interaction revision; and
- export creation uses the selected Enrollment or immutable snapshot
  validator.

The BFF forwards conditional headers only for operations whose generated
policy declares them. `If-Match` participates in the idempotency fingerprint.

### Response and error rules

All public response models use `extra="forbid"`. They are projections, not ORM
serialization. Responses contain stable IDs, version references, timestamps,
reason codes, and links required by the UI, but never raw Meta responses,
tokens, credentials, complete targeting, signed URLs, object keys, or
user-level data.

Additional wire rules are:

- money, ratios that require exact decimal semantics, and confidence bounds
  are canonical decimal strings rather than JavaScript floating-point values;
- timestamps are offset-aware ISO 8601 values;
- account-local dates are explicit date strings plus the frozen account
  timezone;
- lists use opaque cursors bound to Workspace, filters, stable sort keys, and
  immutable snapshot/evaluation identity where relevant;
- all tenant evidence responses use `Cache-Control: private, no-store`;
- large immutable timelines and breakdowns are paginated; and
- Problems contain stable `code`, `trace_id`, `retryable`, and safe field
  errors without echoing sensitive request data.

Every mutable detail response returns all known `allowed_actions` as:

```json
{
  "action": "activate",
  "allowed": false,
  "reason_code": "delivery_config_not_fresh"
}
```

The absence of an action is not interpreted as permission.

## 4.3 Public route matrix

The tables below show paths relative to `/api/v1/scale`.

### Enrollment candidates and lifecycle

| Method | Path | Contract |
| --- | --- | --- |
| `GET` | `/enrollment-candidates` | Paginated, server-owned projection of confirmed Winners and selectable remote binding candidates, including typed eligibility failures and candidate revision |
| `POST` | `/enrollments` | Create a Draft Enrollment from a selected candidate context; requires candidate `If-Match` and idempotency |
| `GET` | `/enrollments` | Paginated Enrollment summaries filtered by lifecycle, mode, maturity, health, or quality state |
| `GET` | `/enrollments/{enrollment_id}` | Full Enrollment projection, current Episode reference, current accepted Evaluation reference, eligibility, and actions; returns ETag |
| `GET` | `/enrollments/{enrollment_id}/eligibility` | Fresh server recomputation of Draft activation or active/resume eligibility, with typed gate results and revision |
| `POST` | `/enrollments/{enrollment_id}/activate` | Recheck all upstream gates and fresh reconciled delivery config under lock; create the first Episode atomically |
| `POST` | `/enrollments/{enrollment_id}/pause` | Fence pending/in-flight live evaluations and retain the binding/current Episode |
| `POST` | `/enrollments/{enrollment_id}/resume` | Recheck eligibility and configuration; continue or roll over the Episode as Section 3 requires |
| `POST` | `/enrollments/{enrollment_id}/close` | Terminally close, fence evaluation, supersede the Episode, and release binding reservation |

`CreateDraftEnrollmentRequest` carries only user choices that cannot be safely
derived from the candidate:

```text
winner_confirmation_id
offer_version_id
market_code
meta_connection_id
remote_binding_candidate_id
```

The server derives Winner decision, AssetVersion, ProductVersion, Landing Page,
account, normalized remote binding, and evidence mode. Redundant browser fields
are forbidden rather than accepted and cross-checked later.

Activate and resume do not perform an unbounded live Meta fetch inside their
transaction. They require a fresh, immutable, reconciled delivery-config
source already owned by the read-side ingestion boundary. When it is absent,
the command fails with a typed eligibility reason and the UI presents the
recovery action.

Pause and close may carry an optional bounded human reason. Activate and resume
have no business body. All four use `If-Match`, `Idempotency-Key`, and durable
command outcomes.

### Episode, evidence, and inspection reads

| Method | Path | Contract |
| --- | --- | --- |
| `GET` | `/enrollments/{enrollment_id}/episodes` | Paginated immutable Episode history and closure events |
| `GET` | `/enrollments/{enrollment_id}/episodes/{episode_id}` | Episode identity, frozen comparability inputs, phase, revisions, current projection cursor, and actions |
| `GET` | `/enrollments/{enrollment_id}/episodes/{episode_id}/evidence` | Paginated terminal-source-manifest and snapshot/evaluation timeline with trigger, acceptance, projection eligibility, mode, watermark, readiness, and quality summary |
| `GET` | `/enrollments/{enrollment_id}/episodes/{episode_id}/source-manifests/{manifest_id}` | Sanitized terminal manifest detail, contract-instance outcomes, safe call/page/reconciliation references, typed source-quality issues, and recovery action; blocked manifests have no snapshot/evaluation link |
| `GET` | `/enrollments/{enrollment_id}/episodes/{episode_id}/evidence/{snapshot_id}` | Sanitized aggregate snapshot detail, metric windows, exact-window values, quality, provenance summaries, and linked Evaluation |
| `GET` | `/enrollments/{enrollment_id}/episodes/{episode_id}/evidence/{snapshot_id}/breakdowns` | Snapshot-stable placement/country/age/gender breakdown page; dimension and cursor are explicit |
| `GET` | `/enrollments/{enrollment_id}/episodes/{episode_id}/unobserved-factors` | Paginated append-only inspection history and correction lineage |
| `POST` | `/enrollments/{enrollment_id}/episodes/{episode_id}/unobserved-factors` | Append an inspection observation under Episode inspection `If-Match` |

There is no unqualified "current breakdown" endpoint. The Enrollment detail
returns the accepted `snapshot_id`/`evaluation_id`; the UI then requests the
breakdown page pinned to that immutable snapshot. A cursor cannot be replayed
against a newer snapshot.

`RecordUnobservedFactorRequest` includes:

```text
factor_type                  # stable enum
observed_at                  # offset-aware timestamp
description                  # bounded, non-causal observation
inspector_notes              # optional bounded notes
reference_object_version_ids # optional safe screenshot/reference lineage
corrects_factor_id           # optional append-only correction link
```

It never accepts a signed URL or raw upload. Screenshot bytes use the existing
upload/reference boundary. Unobserved factors and notes are displayed and
audited but never passed to `evaluate_scale()`.

### Advice, conversion, and TestPlan drafts

| Method | Path | Contract |
| --- | --- | --- |
| `GET` | `/enrollments/{enrollment_id}/advice` | Paginated active and historical threads with independent lifecycle, disposition, snooze, notification, occurrence, conversion, and Outcome summaries |
| `GET` | `/enrollments/{enrollment_id}/advice/{thread_id}` | Full thread timeline and current server-calculated actions; returns interaction ETag |
| `POST` | `/enrollments/{enrollment_id}/advice/{thread_id}/acknowledge` | Append an acknowledged disposition |
| `POST` | `/enrollments/{enrollment_id}/advice/{thread_id}/dismiss` | Append a dismissed disposition and frozen Policy-derived cooldown decision |
| `POST` | `/enrollments/{enrollment_id}/advice/{thread_id}/snooze` | Append or supersede a snooze interval without changing lifecycle/disposition |
| `POST` | `/enrollments/{enrollment_id}/advice/{thread_id}/occurrences/{occurrence_id}/convert` | Idempotently convert the acted-on occurrence through the owning module interface |
| `GET` | `/test-plan-drafts/{draft_id}` | Read generated immutable plan criteria plus editable notes; returns ETag |
| `PUT` | `/test-plan-drafts/{draft_id}` | Replace only `planned_changes` and `observation_notes` using CAS |

Advice conversion always identifies the occurrence. The conversion response
contains the immutable `conversion_id`, acted-on occurrence, output kind, and
exactly one generated `generation_draft_id` or `scale_test_plan_draft_id`.
`refresh_axis` is accepted only when the Advice type and Generation contract
require it. Other Advice types expose contextual deep links instead of a
fabricated conversion.

The TestPlan update schema does not contain generated baselines, success/stop
criteria, Policy checklist, risk, version lineage, or Meta command fields.
Unknown fields fail validation.

There is no user-facing resolve, supersede, expire, notify, or
`create-advice-outcome` command. Those records arise only from accepted system
evidence and trusted owned-module events. Queries expose their lineage.

### Target publication and withdrawal

| Method | Path | Contract |
| --- | --- | --- |
| `GET` | `/target-contexts/current` | Resolve ProductVersion/OfferVersion/market to active/history projection, Target-context revision, impact summary, and actions |
| `GET` | `/targets` | Paginated active and historical Target versions with filters |
| `GET` | `/targets/{target_id}` | Immutable values plus lifecycle projection, impact and actions; returns ETag |
| `POST` | `/target-publication-previews` | Freeze proposed decimals, applicability, stricter-policy checks, affected Enrollments/Advice, context revision, explicit confirmation requirements, preview hash, and expiry |
| `POST` | `/targets` | Consume a matching unexpired publication preview after Owner authorization, MFA step-up, and explicit confirmation |
| `POST` | `/targets/{target_id}/withdrawal-previews` | Freeze withdrawal reason, active dependencies, affected Enrollments/Advice, revision, confirmation requirements, hash, and expiry |
| `POST` | `/targets/{target_id}/withdraw` | Consume a matching unexpired withdrawal preview after Owner authorization, MFA step-up, and explicit confirmation |

Target decimal fields are canonical strings:

```text
target_cpa
breakeven_roas
minimum_economic_spend
optional stricter signal thresholds
```

The preview checks that the Owner cannot weaken the platform Policy and shows
whether publication or withdrawal will request semantic reevaluation and
supersede incompatible Advice. The final command binds Workspace, actor,
action, content hash, context/Target revision, impact hash, confirmation,
authorization epoch, expiry, and one-use MFA assertion JTI.

The browser obtains only an opaque, memory-only step-up grant reference from
the control plane. The BFF validates that grant and injects the signed
server-to-server assertion only for the declared final Target operation. The
assertion never appears in a request body, URL, router state, local storage,
Problem response, telemetry, or audit payload. Audit stores only a safe
evidence/JTI hash.

Publication creates a new immutable `ScaleTargetVersion`; it never edits an
old Target. Withdrawal changes only the separately revisioned lifecycle
projection and emits the durable semantic event required by Section 3.

### Policy and export

| Method | Path | Contract |
| --- | --- | --- |
| `GET` | `/policy/current` | Current published platform ScalePolicyVersion summary |
| `GET` | `/policy/versions/{policy_version_id}` | Historical safe summary needed to explain an Evaluation |
| `POST` | `/exports` | Create an auditable, idempotent sanitized export Operation pinned to an Enrollment, Episode, and optional snapshot/evaluation |
| `POST` | `/exports/{export_id}/download-intents` | Create a short-lived authorized download intent after export completion |

Export generation is asynchronous when packaging can exceed the synchronous
response budget. `POST /exports` returns `202` plus the shared durable
Operation reference. The Operation survives API/Worker restart and produces a
result reference only after the sanitized object is durably committed.

The export Worker reads projection-safe aggregates; it does not load raw Meta
payloads and has no Meta write capability. Export contents are limited to:

- aggregate metrics, intervals, sample sizes, missing/suppressed reasons;
- immutable Episode, Evidence, Evaluation, Advice, Policy, Target, and metric
  contract references;
- user disposition and non-causal Outcome lineage; and
- safe audit references.

Exports exclude credentials, tokens, complete targeting, object keys, signed
URLs, raw adapter responses, user-level data, sensitive account metadata, and
unredacted screenshot content.

## 4.4 UI state model

### Persisted truth and ephemeral browser state

The server is authoritative for all business state. SWR/cache values may
temporarily render a previously validated response, URL query state may hold
navigation/filter choices, component memory may hold an open dialog, and the
existing `IntentTracker` may retain unresolved command identity in local
storage. None of those stores may invent or mutate Enrollment, Episode,
Evidence, health, Advice, Target, conversion, or Outcome truth.

After a refresh, Workspace switch, BFF timeout, or process restart, the UI
reloads projections and reconciles ambiguous commands. It never falls back to
demo seeds, hard-coded sparklines, `window.__mockApi`, or local business
fixtures.

### Orthogonal state axes

The UI does not implement one giant mutually exclusive status enum. It composes
six server-returned axes:

| Axis | Values shown |
| --- | --- |
| Enrollment lifecycle | `draft`, `active`, `paused`, `closed` |
| Episode phase | `collecting`, `evaluating`, `closed` |
| Evidence maturity | `warming`, `monitorable`, `economics_ready`, `insufficient` |
| Current health | `healthy`, `fatigue_watch`, `creative_fatigue`, `economics_decline`, `delivery_anomaly`, `not_delivering`, or unavailable |
| Evidence condition | fresh/stale plus blocking and warning DataQualityIssues |
| Advice state | lifecycle, latest disposition, active snooze, latest notification decision, conversions, and Outcomes as separate fields |

An active Enrollment can therefore be collecting with a quality warning; a
paused Enrollment can retain a current healthy projection; a dismissed thread
can later become resolved; and a snoozed thread can become superseded. The UI
must preserve these combinations instead of forcing one label to erase the
others.

### Enrollment list

`/w/{slug}/scale` supports server-backed filters for lifecycle, evidence mode,
maturity, health, and quality. It has deterministic loading, empty, forbidden,
error, retry, stale, and pagination states.

The empty state links to server-returned enrollment candidates. It contains no
demo enrollment or placeholder chart. Draft cards show the latest eligibility
report and the exact reasons activation is denied.

Each Enrollment card makes the product-contract fields available from
persisted projections:

- exact AssetVersion thumbnail/reference, Winner decision/confirmation,
  Product/Offer/Landing Page, market, and normalized remote binding;
- immutable evidence-mode badge: `customer_sandbox`, `controlled_mock`, or
  `internal_validation`;
- Episode identity, maturity, data watermark, account-local date, timezone,
  and freshness;
- current 7d versus baseline 7d metrics, relative changes, sample sizes, and
  confidence intervals;
- exact-window frequency and reach, never daily-derived approximations;
- blocking/warning DataQualityIssues and typed missing/suppression reasons;
- UnobservedFactor count/latest observation with a link to the inspection
  timeline;
- current health, reason codes, and Policy/Target/metric-contract versions;
- Advice lifecycle/disposition/snooze summaries, conversion links, and Outcome
  count; and
- the fixed disclosure: `MarketAI 不会修改广告或预算`.

To avoid an unbounded list response, the card consumes a bounded current
projection and compact timeline summary. Expanders and detail links load
paginated history. The UI does not issue one request per field or infer a
missing server field.

### Enrollment detail and evidence states

`/w/{slug}/scale/{enrollmentId}` presents:

- stable identity and binding header;
- lifecycle controls rendered from `allowed_actions`;
- Episode and maturity timeline;
- current accepted Evaluation and its immutable snapshot link;
- health reasoning and deterministic signal details;
- DataQualityIssue and UnobservedFactor panels;
- Advice threads and conversion destinations; and
- version/provenance disclosure.

Required render semantics are:

| Condition | UI behavior |
| --- | --- |
| Warming/collecting | Show progress toward monitorable and economics-ready gates; do not imply a negative health conclusion |
| Monitorable | Show delivery/CTR health and list unmet economics gates |
| Economics ready | Show complete eligible metrics, intervals, and target context |
| Healthy | Show stability evidence and exact-window frequency/reach |
| Fatigue watch | Amber health only; explain the first composite hit and that no Advice exists yet |
| Creative fatigue | Show the confirmed multi-signal evidence and active `refresh_creative` thread |
| Economics decline | Show stable CTR beside economic deterioration and Offer/Landing Page review context |
| Delivery anomaly | Show delivery/CPM evidence separately from creative/economic claims |
| Not delivering | Distinguish observed zero delivery from missing data and show progress toward seven complete zero-delivery days; after closure show the system-paused Enrollment and explicit resume eligibility for a new Episode |
| Blocking quality | Show current maturity as insufficient and no current health/new candidate; retain the last accepted projection and historical Advice with an explicit historical/stale label |
| Warning quality | Show Policy-permitted partial evaluation and the exact limitations |
| Stale evidence | Show watermark, frozen freshness deadline, and current thread expiry status; do not silently reuse stale evidence as current |
| Paused | Show that binding is reserved and new live evaluations are fenced; retain historical/current projections |
| Closed | Preserve all history and sanitized export; remove lifecycle and conversion mutations that are no longer eligible |

The page never hides an existing thread merely because the latest evaluation
is blocked. "No Advice" means no new current candidate, not erased history.

### Advice presentation

Each Advice card has independent visual rows or badges for:

1. lifecycle: active, resolved, superseded, or expired;
2. latest disposition: unreviewed, acknowledged, dismissed, or converted;
3. active/superseded snooze interval;
4. notification decision and delivery state;
5. immutable occurrences and severity changes;
6. linked GenerationDraft or ScaleTestPlanDraft; and
7. evidence-backed, explicitly non-causal Outcomes.

Dismissed Advice remains an active thread when lifecycle says active. Snooze
does not overwrite dismissal. Conversion does not assert that the user
executed a remote change or achieved an outcome. Lifecycle transitions are
read-only system events.

Buttons are rendered and enabled only from server `allowed_actions`. A 409
refreshes the authoritative detail and explains the changed eligibility rather
than automatically resubmitting against a new revision.

### Breakdown and privacy presentation

The breakdown page is pinned to one immutable snapshot and clearly labels its
account-local window, watermark, evidence mode, Policy/Target, and contract
versions.

- Placement and country may show spend share, impressions, clicks, purchases,
  and qualifying CPA/ROAS intervals.
- Insufficient segments show permitted aggregate share plus a typed
  insufficient reason.
- Privacy-suppressed segments show a `privacy_suppressed` placeholder and no
  protected metric values. They are not silently omitted, because omission
  would be indistinguishable from missing ingestion.
- Age/gender remain descriptive aggregate shares only.
- No view uses causal language, automatic exclusions, targeting controls, or
  Meta mutation links.

### Target management

`/w/{slug}/scale/targets` is readable by Owner and Member. Members receive
server-denied publication/withdrawal actions. Owners complete:

1. target value entry using exact decimal codecs;
2. server preview and stricter-than-Policy validation;
3. impact review, including affected Enrollments and Advice;
4. explicit second confirmation;
5. control-plane MFA step-up when required; and
6. final idempotent command bound to the preview and revision.

Expired preview, changed impact, stale revision, consumed MFA assertion, and
authorization-epoch changes return typed recovery states. The UI never treats
an old preview as confirmation of new values.

### Accessibility and operational UX

All pages provide:

- semantic headings, tables, lists, and form labels;
- keyboard-complete dialogs and actions with restored focus;
- live-region announcements for mutation results, stale evidence, conflicts,
  and Operation progress;
- text/icon distinctions in addition to color for health and severity;
- responsive layouts that preserve metric labels and interval meaning;
- deterministic skeleton, empty, partial, forbidden, error, retry, and
  reconciliation states; and
- deep links whose Workspace switch invalidates the old gateway cache and
  cannot display cross-Workspace residue.

## 4.5 End-to-end and automated validation

### Deterministic test-control seam

The comprehensive lifecycle scenario uses `controlled_mock`, not customer
Sandbox. `Meta Sandbox` is fixed, persistent simulated data and does not expose
fault injection. Controlled Mock can deterministically select scenario, seed,
source contract, local-day clock, page responses, config observations, and
quality failures.

Scenario control is test-harness-only and out of band from the public Scale
OpenAPI. It creates or advances immutable adapter source revisions and complete
source manifests through the same ingestion boundary production uses. The
browser never receives a fault-injection route, writes an Evaluation directly,
or replaces the evaluator with a UI fixture.

Mock scenario, seed, contract generations, mode, and clock events are frozen
in evidence provenance and evaluation fingerprints.

### Controlled-Mock golden journey

One Playwright journey proves the user-visible contract:

1. Seed a confirmed contextual Winner with Approved AssetVersion, valid
   Offer/Landing Page, controlled-Mock MetaConnection, selectable normalized
   remote binding, and an Owner session with MFA-capable identity evidence.
2. Open the candidate flow, select the Winner/context/binding, and create a
   Draft Enrollment. Assert that evidence mode and authoritative lineage are
   derived, not browser-submitted.
3. Review eligibility and activate using ETag/If-Match. Assert the first
   Episode and binding reservation survive refresh.
4. Advance one complete manifest and verify collecting/warming progress with
   no health conclusion or Advice.
5. Advance a terminal blocked source manifest with a pagination/reconciliation
   defect. Verify `insufficient`, typed issue detail and recovery action, no
   fabricated snapshot/Evaluation or current health/new Advice, and preserved
   historical timeline.
6. Clear the defect through a new source revision and advance enough evidence
   to `monitorable`. Verify delivery/CTR health and unmet economics gates.
7. Publish an economic Target through preview, impact, explicit confirmation,
   test MFA step-up, and an idempotent final command.
8. Advance to `economics_ready` and healthy. Verify exact-window frequency and
   reach, intervals, Policy/Target/contract versions, and no Advice.
9. Advance the first composite fatigue hit. Verify `fatigue_watch`, amber
   explanation, and no AdviceThread.
10. Advance a qualifying distinct complete day. Verify confirmed creative
    fatigue, one AdviceThread, one occurrence, and one notification decision.
11. Dismiss, then snooze the active thread. Verify disposition, cooldown,
    snooze, lifecycle, and occurrence remain independent after refresh.
12. Convert the exact occurrence. Verify one unpaid GenerationDraft with full
    frozen lineage and no Reservation, Provider call, Run, Asset, or Meta call.
13. Navigate to Generation and verify ResolutionPreview, cost confirmation,
    submission, QC, Approved AssetVersion, and new Experiment cannot be
    skipped.
14. Advance two qualifying clean evaluations. Verify the original thread
    resolves through system events even though it was dismissed/snoozed.
15. Advance a separately eligible healthy chain and convert a
    `consider_scale_test` occurrence. Verify one non-executable
    ScaleTestPlanDraft, immutable generated criteria, editable CAS notes, and
    absence of Meta command fields.
16. Complete a controlled, verifiable downstream chain through owned module
    interfaces and assert that the system links a same-mode, non-causal test
    AdviceOutcome. Do not call a user-facing Outcome creation endpoint.
17. Refresh the browser and restart API plus dedicated Scale Worker. Verify
    Enrollment, Episode, evidence, Advice, dispositions, snooze, conversions,
    Operation, and Outcome projections recover without duplicate records.
18. Attempt cross-Workspace, cross-mode, stale revision, duplicate binding,
    and reused-key/different-payload actions. Verify they fail closed and do
    not change the original Workspace.

A network spy and Worker capability spy assert zero real Meta calls and zero
Meta write-port construction throughout the journey.

### Customer-Sandbox smoke journey

A separate smaller Playwright test uses the fixed customer Sandbox adapter to
prove:

- explicit enrollment and activation against persisted Sandbox data;
- refresh and process restart recovery;
- visible `customer_sandbox` provenance on every projection;
- no fallback to controlled Mock or internal validation; and
- any Advice or Outcome remains non-production and same-mode.

The smoke test does not inject faults or rewrite the fixed Sandbox scenario.

### Validation pyramid

Playwright is not the only implementation gate. The following suites are
required.

#### Pure and contract tests

- Section 2 deterministic evaluator vectors and byte-identical results;
- request/response schemas reject Workspace, evidence-mode selection, raw Meta
  data, executable budget/status/bid/targeting/creative fields, and generated
  TestPlan overwrites;
- every route has explicit OpenAPI operation ID, response model, limits,
  idempotency metadata, and conditional-header policy;
- normalized OpenAPI and every generated BFF artifact have zero drift;
- query decoders cover duplicate scalars, repeated enums, malformed cursors,
  cursor/filter/snapshot mismatch, and page bounds; and
- response validators reject secret, raw-payload, object-key, and signed-URL
  fields.

#### Domain and PostgreSQL integration tests

- Owner/Member permissions and Target Owner-only commands;
- RLS and cross-Workspace/mode IDs for every read and mutation family;
- candidate revision, ETag/If-Match 428/400/409 behavior, idempotent replay,
  different-payload key reuse, and actor-scoped command-outcome recovery;
- concurrent activation/resume against the active-or-paused binding unique
  constraint;
- activation/config freshness and withdrawal race closure;
- blocked source manifests, readiness projection ordering, typed recovery
  actions, and the prohibition on fabricating a snapshot/Evaluation;
- Target preview hash, expiry, impact drift, explicit confirmation, one-use MFA
  assertion, authorization epoch, publication, and withdrawal;
- occurrence-specific conversion concurrency and generated TestPlan CAS;
- append-only inspection correction lineage and evaluator exclusion;
- snapshot-stable pagination, privacy suppression, and sanitized export
  packaging; and
- durable export Operation, object commit, download authorization, restart,
  expiry, and audit.

#### Orchestration and security tests

- every Section 3 crash point, lease takeover, projection CAS, replay, expiry,
  notification, rollover, and upstream-withdrawal case;
- AST and transitive import-closure tests prove the Scale API and dedicated
  Worker cannot reach Meta mutation or Launch dispatch;
- deployment manifests omit Meta write tokens and write-side environment
  variables;
- startup assertions reject a Scale process with Meta write capability or an
  over-privileged database role;
- database grants deny Launch/write-side tables and job types;
- OpenAPI contains no Meta mutation route or executable mutation schema; and
- runtime spies prove conversion, export, evaluation, restart, and recovery do
  not construct or call a Meta write adapter.

#### Component and browser tests

- the six state axes and their valid combinations render independently;
- loading, empty, partial, stale, blocked, warning, forbidden, conflict,
  retry, ambiguous-command, paused, and closed states are deterministic;
- `allowed_actions`, not client role/lifecycle inference, controls commands;
- Privacy-suppressed and insufficient segments are distinguishable without
  leaking values;
- Workspace switch clears cache/navigation residue;
- all feature network traffic remains same-origin BFF traffic and CSP blocks
  direct FastAPI calls; and
- Testing Library, keyboard tests, responsive assertions, and `axe-core` pass.

## 4.6 Implementation and cutover order

Implementation proceeds in this order:

1. finish Section 2 and Section 3 persistence/application interfaces;
2. implement Scale Pydantic contracts and the shallow `v1_router`;
3. add command outcomes, ETag/If-Match, action eligibility, Problem handlers,
   Target preview/MFA requirements, and export Operations;
4. generate OpenAPI, allowlist, validators, operation map, paths, and
   `DomainGateway` methods as one atomic seam;
5. build unmounted Scale feature components against the generated gateway;
6. pass pure, domain, RLS, concurrency, recovery, security, component, and BFF
   gates;
7. replace the existing `FeatureEmptyState` on the Pages Router Scale page;
8. pass the controlled-Mock golden journey and customer-Sandbox smoke journey;
   and
9. perform an atomic production cutover with no legacy or fixture-backed Scale
   path left reachable.

The UI remains unmounted until the generated domain seam is complete. A page
that renders real-looking browser fixtures while commands or recovery routes
remain incomplete is not partial completion.

## 4.7 Section completion criteria

Section 4 is complete only when:

- FastAPI, BFF, generated `DomainGateway`, and Pages Router paths match the
  repository architecture;
- every command uses durable intent, idempotency, revision preconditions,
  dual-actor audit, RLS, server authorization, and authoritative recovery;
- Enrollment mode/binding and evaluation inputs cannot be selected by an
  untrusted HTTP caller;
- Target publish/withdraw cannot bypass preview, impact, confirmation, Owner
  authorization, or MFA step-up;
- conversion acts on a specific occurrence and preserves owning-module
  boundaries;
- lifecycle, disposition, snooze, notification, and Outcome remain independent
  in API and UI;
- every evidence/breakdown/export response is snapshot-stable and sanitized;
- blocked/stale states preserve history without presenting it as current;
- terminal source failures remain inspectable with typed recovery actions even
  though they cannot seal a snapshot;
- controlled Mock and customer Sandbox remain visibly, persistently, and
  cryptographically isolated from internal-validation/real evidence;
- refresh, ambiguous responses, and API/Worker restart recover without
  duplicate domain effects; and
- automated evidence proves the Scale API and dedicated Worker have no Meta
  write capability.

## 4.8 Explicit non-responsibilities

The Domain Interface and UI do not:

- accept or infer an executable Meta budget, bid, targeting, placement,
  creative, status, pause, enable, or delete command;
- expose a route that resolves, supersedes, expires, or fabricates Advice or
  AdviceOutcome on user assertion;
- let the browser select source manifests, metric contracts, Policy, Target,
  algorithm, evidence mode, adapter, account, or current Evaluation;
- persist MFA assertions, signed URLs, credentials, raw Meta payloads, or
  complete targeting in browser-accessible state;
- treat a disposition, snooze, note, screenshot, or ScaleTestPlan as proof of
  remote execution or business outcome;
- render breakdown correlation as causation or create demographic exclusions;
- use customer Sandbox as a fault-injection framework;
- substitute one large Playwright test for domain, RLS, concurrency, recovery,
  code-generation, accessibility, export, and capability-separation tests; or
- mount an App Router feature, legacy route, direct FastAPI client, or
  browser-owned domain store.
