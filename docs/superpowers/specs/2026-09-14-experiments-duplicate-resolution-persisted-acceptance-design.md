# Issue #88 — Complete Experiments Duplicate Resolution and Persisted Browser Acceptance

> **Generated**: 2026-09-14
> **Status**: Design candidate — not yet approved for implementation.
> **Parent issue**: #28 (v9 Experiments umbrella)
> **Prior art**: #22 schema-2 model (immutable contextual experiment decisions), #79 (Experiments workspace UI — route-mocked)

---

## Overview

Issue #79 built the Experiments workspace UI but left duplicate-concept resolution unwired (freeze sends only `expected_revision`, not `concept_resolution_plan`) and used a JavaScript-closure route mock as acceptance evidence. This design completes the gap:

1. **Backend**: A read-only duplicate-analysis endpoint, shared candidate-loading/detection seam, and frozen resolution evidence on ExperimentDetail.
2. **Frontend**: A duplicate-resolution panel in the freeze flow, freeze submission with `analyzed_candidate_set_hash` and `concept_resolution_plan`, and frozen evidence display.
3. **E2E**: Replace route-mocked Playwright tests with real PostgreSQL/FakeMetaAdapter browser journeys exercising the full lifecycle.

Central test-seam rule: setup may manufacture inputs that the UI cannot create (seed entries, inject metrics, configure adapter faults), but every outcome under acceptance — analysis, freeze, resolution evidence, launch, reconciliation, judgement, and confirmation — must be produced through production behavior.

---

# Section 1: Backend

## §1.1 Duplicate-analysis endpoint

### Routes

**Private domain route** (FastAPI, internal network):
```
GET /api/v1/experiments/{experiment_id}/duplicate-analysis
```

Workspace identity comes exclusively from the verified `TenantContext` attached by the BFF — never from a URL path segment. The domain service scopes all queries by `context.workspace_id`.

**BFF mapping** (Next.js, same-origin to browser):
```
GET /api/domain/v1/w/{workspace_slug}/experiments/{experiment_id}/duplicate-analysis
```

BFF resolves `workspace_slug` → `workspace_id`, verifies membership, attaches TenantContext claims, and proxies to the private domain route. The BFF allowlist must include this GET route.

**BFF status semantics:**
- Unknown slug → **404** (slug resolution fails before domain call)
- Known Workspace without membership → **403** (membership check fails)
- Foreign `experiment_id` within an authorized Workspace → domain **404** (experiment not found under `context.workspace_id`)

Read-only. No request body. Authorization: any Workspace member (Owner or Member).

### Response: `DuplicateAnalysisResponse`

| Field | Type | Description |
|-------|------|-------------|
| `experiment_id` | `str` | The experiment being analyzed |
| `analyzed_revision` | `int` | The experiment's revision at analysis time |
| `candidate_set_hash` | `str(64)` | SHA-256 fingerprint of the resolved candidate set (source entries + concept keys) |
| `has_duplicates` | `bool` | Whether any duplicate-concept groups exist |
| `duplicate_groups` | `list[DuplicateGroup]` | Deterministically ordered groups |

### `DuplicateGroup`

| Field | Type | Description |
|-------|------|-------------|
| `duplicate_group_hash` | `str(64)` | SHA-256 of `(dimension_key, concept_key_id)` — canonical identity |
| `dimension_key` | `str` | The dimension (e.g., "headline", "hook") |
| `concept_key_id` | `str` | The concept key ID |
| `concept_key_name` | `str` | Display name (excluded from all hashes) |
| `members` | `list[DuplicateGroupMember]` | Deterministically ordered members |
| `allowed_actions` | `list[str]` | Subset of `["allow_controlled_duplicate", "exclude_source_candidate", "remap_before_freeze"]` — see §1.3 |
| `remap_targets` | `list[RemapTarget]` | Valid remap destinations (deterministically ordered by ID); empty when remap not in `allowed_actions` |

### `DuplicateGroupMember`

| Field | Type | Description |
|-------|------|-------------|
| `source_entry_id` | `str` | Draft entry ID (presentation reference) |
| `source_candidate_fingerprint` | `str(64)` | Stable SHA-256 fingerprint for stale detection and action targeting |
| `asset_version_id` | `str` | The asset version this entry references |
| `display_label` | `str` | Human-readable label (excluded from all hashes and identity) |

### `RemapTarget`

| Field | Type | Description |
|-------|------|-------------|
| `concept_key_id` | `str` | Target concept key ID |
| `concept_key_name` | `str` | Display name |

### Ordering

- Groups: sorted by `(dimension_key, concept_key_id)`.
- Members within group: sorted by `source_candidate_fingerprint`.
- Remap targets: sorted by `concept_key_id`.

All presentation fields (`display_label`, `concept_key_name`) are excluded from hashes and ordering identity.

## §1.2 Shared candidate-loading and detection seam

Extract from `_apply_concept_resolution_plan` a shared internal path used by both the analysis endpoint and `freeze_experiment_v2`:

1. **Load/resolve current draft candidate sources** → `list[FrozenCandidateInput]` with concept rows attached.
2. **Calculate source fingerprints** — deterministic, canonical.
3. **Compute `candidate_set_hash`** — `_hash_json(sorted(source.source_fingerprint for source in sources))`. The source fingerprint already covers the source entry, immutable AssetVersion content, execution mode, marketing variables, and sorted concept identities/value hashes; presentation labels remain excluded.
4. **Detect duplicate groups** — pure function once it receives resolved candidates. Returns groups with deterministic ordering and hashes.

The detector is pure: no mutations, no database writes. Both analysis and freeze cross this same seam. Freeze remains responsible for applying the resolution plan, validating it globally, and persisting records.

### Files touched

- `app/experiments/service.py` — extract `_load_and_resolve_candidates()` and `_detect_duplicate_groups()` from `_apply_concept_resolution_plan`. Add `analyze_duplicates()` method. Existing freeze validation continues to call the same detection logic.
- `app/experiments/v1_router.py` — add `GET /{experiment_id}/duplicate-analysis` endpoint.
- `app/experiments/response_models.py` — add `DuplicateAnalysisResponse`, `DuplicateGroup`, `DuplicateGroupMember`, `RemapTarget`.

## §1.3 Action eligibility

`allowed_actions` per group is determined by group membership size and workspace concept-key availability:

- **`allow_controlled_duplicate`**: always available. User provides a reason; all members survive as frozen candidates.
- **`exclude_source_candidate`**: available when group has exactly 2 members. Requires selecting one member's `source_candidate_fingerprint` as the excluded candidate. Surviving count after all exclusions across all groups must be ≥ 2 (validated globally at freeze).
- **`remap_before_freeze`**: available when group has exactly 2 members AND valid remap targets exist in the workspace. Requires selecting one member's `source_candidate_fingerprint` and a `remap_concept_key_id`. Post-remap collision check: remap must not create new unresolved duplicates (validated globally at freeze).

For groups with >2 members, only `allow_controlled_duplicate` is offered — exclude and remap target a single member, which is insufficient to resolve a group of three or more.

Valid remap targets: platform-scoped concept keys (where `workspace_id IS NULL`) plus current-workspace concept keys, excluding the group's current `concept_key_id`. Deterministically ordered by `concept_key_id`.

## §1.4 Freeze request extension

The freeze request gains one new field:

```python
class FreezeExperimentRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    analyzed_candidate_set_hash: str = Field(min_length=64, max_length=64)
    concept_resolution_plan: list[ConceptResolutionActionRequest] = Field(default_factory=list)
```

Each `ConceptResolutionActionRequest` item submits:
- `duplicate_group_hash` — group identity
- `action` — one of the three allowed actions
- `reason` — 1–2000 chars, required
- `source_candidate_fingerprints` — sorted member fingerprints for stale-group-membership detection
- `source_candidate_fingerprint` — singular, the affected candidate for exclude/remap (required for those actions)
- `remap_concept_key_id` — target concept key for remap (required for remap action)

Freeze recomputes `candidate_set_hash` under locks and compares to `analyzed_candidate_set_hash`. Mismatch → `concept_resolution_stale`.

## §1.5 Error taxonomy

| Error code | Condition | HTTP |
|------------|-----------|------|
| `revision_conflict` | `expected_revision` does not match current experiment revision (concurrent edit) | 409 |
| `concept_resolution_stale` | `analyzed_candidate_set_hash` does not match recomputed hash (candidates changed) OR `source_candidate_fingerprints` do not match current group members | 409 |
| `concept_unresolved` | Duplicate groups exist but submitted plan is incomplete (missing groups) | 422 |
| `concept_resolution_invalid` | Plan references unknown hashes, ineligible actions, or remap creates new collisions | 422 |

These are distinct Problem Details codes. The current router's collapsed `experiment_conflict` is mapped to the specific codes above.

## §1.6 Frozen resolution evidence on ExperimentDetail

### When populated

Whenever an `ExperimentVersionRecord` exists (schema-2 experiments) — regardless of whether the experiment is frozen, running, or closed. Null only when no immutable schema-2 version exists.

### Shape

```python
class ConceptResolutionEvidence(BaseModel):
    concept_resolutions_hash: str  # from ExperimentVersionRecord
    had_duplicates: bool
    resolutions: list[ResolutionEvidenceRow]  # sorted by duplicate_group_hash
```

```python
class ResolutionEvidenceRow(BaseModel):
    duplicate_group_hash: str
    dimension_key: str
    concept_key_id: str
    concept_key_name: str  # display, outside hash
    action: str
    reason: str
    affected_candidate_fingerprint: str | None  # for exclude/remap
    remap_destination_concept_key_id: str | None  # for remap
    remap_destination_concept_key_name: str | None  # display, for remap
    actor_role: str
    resolved_at: datetime  # OpenAPI `date-time`; generated TypeScript value is an ISO 8601 string
```

For a frozen version with no duplicates: `{ concept_resolutions_hash: "...", had_duplicates: false, resolutions: [] }`.

### Source of truth

- `ExperimentVersionResolutionRecord` is the query/read projection shown in ExperimentDetail.
- `ExperimentVersionRecord.concept_resolutions_hash` verifies the complete ordered set.
- `frozen_snapshot_json` is immutable audit evidence, not a separately editable projection.

### Three-way runtime invariant

The detail-read path performs a three-way consistency check:

1. Recompute `concept_resolutions_hash` from the normalized `ExperimentVersionResolutionRecord` rows (sorted by `duplicate_group_hash`, using only semantic hash inputs).
2. Compare the recomputed hash to `ExperimentVersionRecord.concept_resolutions_hash`.
3. Independently canonicalize the `concept_resolution_plan` embedded in `frozen_snapshot_json` and compare its hash to the same `concept_resolutions_hash`.

Any mismatch among the three → fail closed (500, integrity violation logged with the divergent representations). This is a runtime invariant, not test-only. The test suite additionally verifies this invariant explicitly with a simulated row edit → 500.

### Evidence row content

Exposes only UI-safe authorization evidence: `actor_role` and `resolved_at`. Does not expose the complete internal JSON field. `affected_candidate_fingerprint` identifies which candidate was excluded or remapped. `remap_destination_concept_key_id` and `remap_destination_concept_key_name` describe the remap target.

### Immutable display name source

`concept_key_name` and `remap_destination_concept_key_name` must be resolved from frozen records, not the live concept-key registry. A later concept-key rename in the workspace must not change supposedly immutable evidence display.

The exact immutable sources:
- **`concept_key_name`**: read directly from `ExperimentVersionConceptRecord.concept_key_name`, the frozen column written for candidates in the duplicate group.
- **`remap_destination_concept_key_name`**: read from a new `to_concept_key_name` value written into `ExperimentVersionResolutionRecord.outcome_evidence_json` when the remap is applied. It is presentation evidence and remains excluded from `concept_resolutions_hash`.

The detail-read path joins these frozen records — never the live `dimension_concept_keys` registry. Both the frozen concept column and `outcome_evidence_json` are write-once data on immutable records.

### Canonical hash inputs

The following normalized fields feed `concept_resolutions_hash` (sorted by `duplicate_group_hash`):
- `duplicate_group_hash`
- `dimension_key`
- `concept_key_id`
- `action`
- `reason`
- `source_candidate_fingerprints` (sorted)
- `affected_candidate_fingerprint` (nullable)
- `remap_concept_key_id` (nullable)

Excluded: `concept_key_name`, `remap_destination_concept_key_name`, `display_label`, `actor_role`, `resolved_at`. These are projected outside the semantic hash.

### Files touched

- `app/experiments/response_models.py` — add `ConceptResolutionEvidence`, `ResolutionEvidenceRow` to `ExperimentDetail`.
- `app/experiments/service.py` — populate evidence when building detail response. Add runtime hash-recomputation invariant.

## §1.7 Contract artifacts

- OpenAPI spec regeneration (after endpoint and model changes).
- BFF allowlist/policy update for `GET .../duplicate-analysis` route.
- Generated TypeScript client and validators regeneration.
- Response validation tests for the new endpoint and extended detail.

## §1.8 Required backend tests

1. Analysis and freeze produce byte-identical canonical detection payloads (complete response objects contain presentation-only labels that differ).
2. Stable ordering and hashes across calls with identical inputs.
3. No-duplicate response: `has_duplicates: false`, empty groups.
4. Stale revision between analysis and freeze → `revision_conflict` (concurrent entry edit increments revision).
5. Current revision + obsolete `analyzed_candidate_set_hash` → `concept_resolution_stale`.
6. Stale `source_candidate_fingerprints` within a group → `concept_resolution_stale`.
7. Remap producing a new collision is rejected → `concept_resolution_invalid`.
8. Cross-Workspace access rejected at analysis and freeze without leaking entries.
9. Frozen/running/closed details retain identical resolution evidence.
10. No-duplicate frozen version returns explicit empty evidence (`had_duplicates: false`, `resolutions: []`).
11. Three-way invariant: normalized resolution rows, `concept_resolutions_hash`, and the `concept_resolution_plan` in `frozen_snapshot_json` all agree. Verified at runtime (§1.6) and tested explicitly.
12. Three-way invariant catches tampering: simulated resolution-row edit → 500; simulated snapshot-plan edit → 500.
13. Action eligibility: groups >2 members offer only `allow_controlled_duplicate`.
14. Remap targets include platform + workspace keys, exclude current concept key.

---

# Section 2: Frontend

## §2.1 Pre-freeze analysis trigger

When the user opens "Review & freeze" for a schema-2 draft experiment with ≥ 2 entries, the UI calls `GET /{experiment_id}/duplicate-analysis` via `useDomainQuery` (not `useDomainCommand` — this is a GET, read-only operation).

The analysis query is gated: freeze is only available when the analysis has succeeded and `analyzed_revision` matches the currently displayed experiment revision. The UI exposes loading, failure, and explicit retry states for the analysis call.

### Files touched

- `web/hooks/experiments/useExperimentsWorkspace.ts` — add `analyzeDuplicates` query using generated client.
- DomainGateway and `useDomainQuery` read-operation switch files — register the new GET route.

## §2.2 Duplicate resolution panel

### Layout

The resolution panel is placed in the **main content column** immediately after the Candidates section — not in the sidebar. The sidebar retains: freeze readiness indicator, acknowledgement checkbox, and Freeze button. This avoids cramping potentially long multi-group resolution in the narrow sidebar.

### Component: `DuplicateResolutionPanel`

Renders when `has_duplicates: true`. For each group:

- **Group header**: dimension key + concept key name (e.g., "Headline — Bold")
- **Member list**: each member shows `display_label` and `asset_version_id` (truncated). Members are identifiable so the user understands which candidates conflict.
- **Action selector**: radio group of `allowed_actions`. For 2-member groups: exclude/allow/remap as applicable. For >2-member groups: only `allow_controlled_duplicate`.
- **Affected candidate selector**: shown for `exclude_source_candidate` and `remap_before_freeze`. A dropdown or radio of group members, selecting one `source_candidate_fingerprint`. Both actions require the user to choose which member is affected.
- **Reason**: required text input (1–2000 chars).
- **Remap target dropdown**: shown only when `remap_before_freeze` is selected. Populated from `remap_targets[]` returned by analysis. No free-text entry.

### Panel state management

State is keyed by `experiment_id + analyzed_revision + candidate_set_hash`. When any part of this identity changes (re-analysis returns different data), both the resolution state and the acknowledgement checkbox are reset. The user must re-resolve from scratch.

The freeze button enables only when:
1. Analysis succeeded.
2. `analyzed_revision` matches displayed experiment revision.
3. Every group has a valid action + reason (+ affected candidate where required, + remap target where required).
4. Acknowledgement checkbox is checked.

### Files touched

- `web/components/experiments/DuplicateResolutionPanel.tsx` — new component.
- `web/components/experiments/ExperimentsWorkspace.tsx` — integrate panel in main column after Candidates, wire readiness to sidebar.

## §2.3 Freeze submission

The freeze call sends:

```typescript
{
  expected_revision: analysis.analyzed_revision,
  analyzed_candidate_set_hash: analysis.candidate_set_hash,
  concept_resolution_plan: groups.map(g => ({
    duplicate_group_hash: g.duplicate_group_hash,
    action: g.selectedAction,
    reason: g.reason,
    source_candidate_fingerprints: g.members.map(m => m.source_candidate_fingerprint).sort(),
    source_candidate_fingerprint: g.affectedCandidateFingerprint ?? undefined,
    remap_concept_key_id: g.remapTargetId ?? undefined,
  }))
}
```

Uses `source_candidate_fingerprints` (sorted member fingerprints) for membership stale detection, plus the singular `source_candidate_fingerprint` for the affected candidate in exclude/remap. Entry IDs remain presentation-only.

### Error handling

- **`revision_conflict`**: Reset the retained freeze command. Refresh both experiment detail and analysis. Focus a change notice explaining the experiment was modified. Require new acknowledgement.
- **`concept_resolution_stale`**: Same reset flow — refresh detail + analysis, clear resolution state, focus change notice, require new acknowledgement.
- **Network uncertainty** (timeout, connection error): retain the existing reconciliation behavior. Do not treat as stale.
- **`concept_unresolved` / `concept_resolution_invalid`**: Display the specific error. These indicate a UI logic bug (plan should always be complete when the button is enabled) but the user should see a clear message, not a silent failure.

### Files touched

- `web/components/experiments/ExperimentsWorkspace.tsx` — update freeze submission (lines ~693–699), add error handlers.
- `web/hooks/experiments/useExperimentsWorkspace.ts` — update freeze command signature.

## §2.4 Frozen resolution evidence display

### Component: `FrozenResolutionEvidence`

Rendered in the experiment detail view whenever `concept_resolution_evidence` is non-null. Appears as a "Concept Resolution" section in the main column.

- **`had_duplicates: true`**: A summary card per resolution row. Each card shows:
  - Dimension + concept key name
  - Action taken (human-readable label)
  - Reason text
  - Affected candidate (for exclude: which candidate was excluded; for remap: which candidate was remapped and the destination concept)
  - Actor role + resolved timestamp
  - Read-only — matches immutable evidence.
- **`had_duplicates: false`**: A single line: "No duplicate concepts detected at freeze time."
- Renders identically across frozen/running/closed experiment states.

### Files touched

- `web/components/experiments/FrozenResolutionEvidence.tsx` — new component.
- `web/components/experiments/ExperimentsWorkspace.tsx` — integrate evidence display in detail view.

## §2.5 Generated client and gateway wiring

- Regenerate TypeScript client from updated OpenAPI spec (adds `DuplicateAnalysisResponse` types and `GET .../duplicate-analysis` operation).
- Register the new route in DomainGateway and `useDomainQuery` read-operation switch.
- No manual type authoring — all types flow from the OpenAPI spec.

---

# Section 3: E2E — Persisted Browser Acceptance

## §3.0 Test-seam rule

Setup may manufacture inputs that the UI cannot create (seed draft entries, inject metric observations, configure adapter faults), but every outcome under acceptance — analysis, freeze, resolution evidence, launch, reconciliation, judgement, and confirmation — must be produced through production behavior.

Fixtures must not inject proposals, decisions, resolution rows, or Operation outcomes. Seed metrics, then invoke the real judgement flow to produce the proposal.

## §3.1 Infrastructure

Follow Localization's e2e pattern:

- **Local/Compose PostgreSQL preflight**: verify database is running before tests start.
- **Explicit mutation opt-in**: tests that modify server state declare it.
- **Restart helpers**: functions to restart API and Worker processes for recovery testing.
- **Unique fixture identities**: each test run uses unique workspace/user identities to avoid cross-test contamination.
- **One Playwright worker** for container-restart scenarios (cannot parallelize process restarts).
- **Guarded runner**: `test:e2e:experiments` fails preflight if PostgreSQL/API prerequisites are not met — this is acceptance evidence, not optional. An umbrella suite (e.g., `test:e2e:all`) may omit these tests from a lightweight CI matrix, but the dedicated experiments runner must fail, not skip.

### Real backend stack

- PostgreSQL with RLS-enforced tenant isolation.
- API server with schema-2 experiment support.
- Worker with FakeMetaAdapter for both `customer_sandbox` and `controlled_mock`; only the latter receives immutable fault configuration. `internal_validation` remains read-only and cannot execute Launch writes.
- No route interception for domain endpoints — Playwright drives the SaaS UI through the real BFF.

### Deterministic failure injection

Per ADR 0009, fault injection belongs to the decision-ineligible `controlled_mock` evidence mode, not `customer_sandbox`. The `customer_sandbox` golden path must remain fault-free. Scenarios requiring deterministic faults (Scenarios 4, 5, 9) use a separate `controlled_mock` Launch path with fault configuration.

**Existing infrastructure — no new table or router needed:**

`ScenarioVersionRecord.scenario_config["faults"]` is already persisted, tenant/mode-scoped, and passed exclusively to `controlled_mock` adapters via `credential_resolution.py:104`:
```python
fault_config = scen.scenario_config.get("faults") if meta_mode == "controlled_mock" else None
adapter = FakeMetaAdapter(..., fault_config=fault_config)
```

`FakeMetaAdapter` already accepts `fault_config` in its constructor and stores it as `self._fault_config`, but never reads it during operations. Extend `ensure_object()` to consume that existing configuration. There is no separate `ensure_step()` interface.

**Fault config structure** (the complete persisted `scenario_config`):
```json
{
  "faults": {
    "rules": [
      {
        "object_type": "ad",
        "match_ordinal": 2,
        "effect": "uncertain_after_apply"
      }
    ]
  }
}
```

`match_ordinal` is one-based among `ensure_object()` calls of the selected `object_type` for the scenario version. Each E2E case receives a unique scenario version, so the ordinal is deterministic. This lets an immutable scenario target, for example, the final candidate Ad without knowing the preview's generated idempotency key in advance. Effects that need data accept only their relevant fields: `pause_before_apply` requires a bounded `duration_ms`; `return_state_patch` requires a `state_patch` object. A state patch may not modify `status`, and the adapter still enforces `PAUSED`. Unknown fields or effects fail closed during scenario construction.

Supported deterministic effects:

- `uncertain_before_apply`: raise after the durable call-intent dispatch marker but before creating the fake remote object. Reconciliation read-back returns not found.
- `uncertain_after_apply`: create and flush the fake remote object, then raise. The saga's `ProvisioningUncertain` handler durably commits the partial state, so reconciliation read-back finds the object.
- `pause_before_apply`: block for the configured `duration_ms` inside `ensure_object()` after the durable dispatch marker and before creating the fake object. One restart test lets the bounded pause expire; another terminates the Worker before expiry to create a deterministic post-dispatch/no-outcome checkpoint.
- `return_state_patch`: return success while applying a configured state patch to the fake remote object. Tests use a budget mismatch while preserving `status="PAUSED"` to exercise a definitive parity failure without violating write-safety.

Effects are deterministic functions of the immutable scenario version, object type, and idempotency key. Customer Sandbox never receives this configuration.

**E2E fixture setup**: extend the existing internal `connections.py:create_connection()` interface with an optional initial `scenario_config` used when atomically creating the immutable default scenario. The public connection request does not expose arbitrary fault configuration. A guarded E2E seed script calls this interface to create a `controlled_mock` connection with its fault rules before the browser journey begins. No new migration, table, router, or token is needed.

### Controlled-mock Launch admission

Preview construction and the underlying Launch module understand all three Meta modes, and the BFF already forwards the request's `meta_mode`. Execution is narrower today: `execute_multi_candidate_preview_v1()` rejects every mode except `customer_sandbox`, while the Experiments UI hard-codes `customer_sandbox`.

**Changes needed for controlled-mock E2E:**

1. **Execute admission** (`app/launch/api.py`): allow `customer_sandbox` and `controlled_mock` through the existing execute endpoint; continue rejecting `internal_validation`, which is read-only. The preview, connection, binding, job context, and provisioning record must all carry the same immutable mode, enforced by the existing composite lookups and validated again at execute time.
2. **FakeMetaAdapter** (`app/adapters/meta/fake_adapter.py`): consume `_fault_config` in `ensure_object()` and implement the four deterministic effects above.
3. **UI mode selection**: replace the hard-coded constant with a selector containing `customer_sandbox` (default) and `controlled_mock` (clearly labeled as decision-ineligible test evidence). The current UI accepts a mode-scoped connection ID rather than listing connections, so it does not claim to infer whether both modes exist. The server remains authoritative and rejects a connection or preview whose immutable mode differs from the submitted mode.
4. **Read queries**: pass the selected mode through preview and provisioning queries instead of hard-coding `customer_sandbox` in `useMetaLaunchPreview()` and `useMetaLaunchProvisioning()`.

### Files touched

- `web/e2e/experiments/fixtures.ts` — replaced: old route-mock content removed, new real-backend fixtures.
- `web/e2e/experiments/helpers.ts` — new: workspace provisioning, authenticated context, API inspection helpers, restart helpers.
- `web/e2e/experiments/playwright.config.ts` or equivalent — runner configuration with preflight failure.
- `app/meta/connections.py` — accept the immutable initial scenario configuration on the internal connection-construction interface.
- `app/adapters/meta/fake_adapter.py` — consume `_fault_config` during `ensure_object()`.
- `app/launch/api.py` — admit mode-matched `controlled_mock` previews through the existing execute endpoint.
- `web/components/experiments/ExperimentsWorkspace.tsx` — add the mode selector.
- `web/hooks/experiments/useExperimentsWorkspace.ts` — pass the selected mode to preview/provisioning reads.

## §3.2 Test scenarios

### Scenario 1: Duplicate resolution → freeze → PAUSED Sandbox Launch → evidence → proposal → human Winner (golden path)

1. Seed a schema-2 draft experiment with entries sharing concept keys (produces duplicate groups).
2. Open "Review & freeze" — analysis returns groups.
3. Resolve all groups (exercise each action type across groups: exclude one member, allow-with-reason another group, remap a third).
4. Acknowledge and freeze.
5. Verify frozen snapshot includes resolution evidence (evidence section visible, correct actions/reasons/affected candidates).
6. Preview Launch — verify exact candidate manifest, budget, currency, `customer_sandbox` mode, PAUSED invariant displayed at confirmation.
7. Confirm Launch — verify Operation progress transitions.
8. Wait for provisioning — verify all FakeMetaAdapter objects created.
9. Bind frozen candidates to resulting ad IDs.
10. Start experiment.
11. Seed metric evidence (via fixture — manufactured input).
12. Run judgement through the UI (production behavior) — verify statistical proposal appears.
13. Verify proposal is NOT a confirmed Winner; no ExperimentDecisionRecord or ContextualVerdictProjectionRecord written yet.
14. Acknowledge proposal → human confirm Winner.
15. Verify append-only ExperimentDecisionRecord and ContextualVerdictProjectionRecord written (NOT onto AssetVersion — AssetVersion remains immutable).
16. Browser refresh at durable milestones (after freeze, after provisioning, after judgement, after confirmation) — state recovered from PostgreSQL.

### Scenario 2: Stale plan detection

Two sub-cases:

**2a: Stale revision (concurrent entry edit)**
1. Create experiment with duplicates, open analysis.
2. Mutate an entry server-side (e.g., add an entry) — this increments the experiment revision.
3. Submit freeze with stale `expected_revision` → `revision_conflict`.
4. Verify UI resets: re-fetches detail + analysis, clears resolution state and acknowledgement, shows change notice.

**2b: Stale candidate set hash — service-level precondition test only**

All entry mutations (add/update/remove) increment the experiment revision, and concept-key names are excluded from the candidate hash. Bound AssetVersion/RunItem inputs are immutable. No current upstream mutation path produces a revision-current but hash-stale state in the browser.

This sub-case is therefore tested as a **synthetic service-level precondition test** (not a browser scenario):
- Directly call `freeze_experiment_v2` with a valid `expected_revision` but a fabricated `analyzed_candidate_set_hash` that does not match the recomputed hash.
- Assert `concept_resolution_stale` is returned.
- This proves the guard is wired correctly and will catch any future upstream path that changes hash-bearing inputs without incrementing revision.

The browser scenario is removed — there is no realizable user journey that produces this state.

### Scenario 3: Inconclusive judgement

1. Freeze and launch experiment through golden path.
2. Seed metric evidence with insufficient data for statistical significance.
3. Run judgement through the UI — proposal returns inconclusive.
4. Verify inconclusive does NOT create ExperimentDecisionRecord or ContextualVerdictProjectionRecord.
5. Verify no downstream Winner record exists.
6. Refresh — inconclusive state persists.

### Scenario 4: Uncertain dispatch → reconciliation → no duplicate objects

Uses a `controlled_mock` Launch path with fault injection (per §3.1).

The saga dispatches steps sequentially. If a step produces uncertainty, subsequent steps are never dispatched. Recovery (`execute_recovery`) classifies uncertain intents via adapter read-back but **never resumes `execute_provisioning`** — it only resolves the classification of already-dispatched attempts.

1. Freeze experiment and confirm Launch under `controlled_mock` evidence mode. Seed `ScenarioVersionRecord` with `uncertain_after_apply` on the final expected write step (`create_ad` for the final candidate).
2. Saga dispatches steps in order. All steps before the faulted step complete normally. The faulted step returns uncertain — saga stops, Operation enters `uncertain` state.
3. Verify Operation is `uncertain`. Steps after the faulted step were never dispatched.
4. Create reconciliation and execute recovery through the UI. Recovery performs adapter read-back on the uncertain step:
   - **Object exists** (adapter created it before the uncertain response): resolution classifies the step as succeeded.
   - **Object does not exist**: resolution classifies the step as failed.
5. If read-back confirms every expected write exists, recovery performs **read-only parity reconstruction**: it records recovery inspection intents/outcomes while reading the Campaign, AdSets, and Ads, verifies the frozen manifest, budget, configuration, and PAUSED status, then appends a `post_provisioning` `LaunchParityCheckRecord` whose detail identifies reconciliation as its source. `post_provisioning` is an existing allowed `check_type`; no schema change is needed. Recovery never calls `ensure_object()`.
6. `_derive_manifest_outcome` then observes the passed `post_provisioning` parity record. Therefore:
   - Confirmed final write + complete manifest + passed reconstructed parity → `succeeded` → `provisioned`.
   - Missing final write, incomplete manifest, or failed reconstructed parity → `partially_succeeded` → `partially_provisioned` (terminal).
7. Verify no duplicate Meta objects: reconciliation uses `read_object()` only and never replays the original write.
8. Assert via `FakeMetaObjectRecord`, `MetaCallIntentRecord`, and the appended parity record — not browser network observation.

Note: If the faulted step is not the final expected write, the manifest contains un-dispatched subsequent writes. Recovery does not reconstruct parity or dispatch them. The outcome is `partially_succeeded` → `partially_provisioned` (terminal). A fenced write-continuation mechanism is out of scope for this issue.

### Scenario 5: Restart recovery (three distinct checkpoints)

Uses a `controlled_mock` Launch path.

**5a: API restart — durable reads and UI recovery while Worker continues**
1. Freeze and confirm Launch under `controlled_mock` with a bounded `pause_before_apply` fault. Poll until the durable dispatch marker exists.
2. Restart the API server while the Worker is independently paused inside the adapter call.
3. Verify the UI recovers after API restart: experiment detail, Operation progress, and frozen evidence are all readable from PostgreSQL. The API restart does not affect the Worker — the Worker continues independently.
4. The bounded pause expires and the Worker completes provisioning. Verify final `provisioned` state via UI refresh.

**5b: Worker restart after lease but before dispatch — expired leased work safely retries**
1. Stop the normal Worker before confirming Launch, then confirm Launch so the API durably enqueues one `launch_provisioning` Job.
2. Run a guarded one-shot E2E Worker checkpoint helper in the Worker container. It calls the production `jobs.claim_next(owner, lease_seconds=<short bounded value>)`, verifies that it claimed the exact expected Launch Job in state `leased`, and exits immediately without calling `jobs.mark_running()` or a Job handler. It must fail if it claims a different Job. This uses the real queue API and does not mutate the Job row directly.
3. Assert that the Job is `leased`, the Operation is still `queued`, and no `MetaCallIntentRecord.network_dispatched_at` marker exists. This is the only safe crash window for a Launch Job whose `safe_to_retry_after_crash` flag is false.
4. Wait for the bounded lease to expire, then start a normal Worker. The production PostgreSQL claim/recovery path requeues the expired `leased` Job regardless of `safe_to_retry_after_crash`; the Worker claims it again, calls `mark_running()`, and executes it once.
5. Verify provisioning completes with no duplicate objects. Assert the Job audit history contains the initial lease, `job.requeued` with `lease_expired`, and the second lease, and that each `FakeMetaObjectRecord` was created exactly once.

The checkpoint helper is a test-runner process, not a domain route or product fault hook. It exercises `app.platform.jobs.claim_next()` and exits before the dispatch boundary; `web/e2e/experiments/fixtures.ts` supplies the exact expected Job id obtained from the persisted Operation/Job association.

**5c: Worker restart after dispatch — uncertain requires reconciliation**

This checkpoint uses `pause_before_apply` on the first write to create a deterministic window after the durable dispatch marker but before any fake remote object is created.

1. Freeze and confirm Launch under `controlled_mock`; configure `pause_before_apply` on `create_campaign`.
2. Poll until the `MetaCallIntentRecord.network_dispatched_at` marker exists and no outcome exists.
3. Terminate and restart the Worker during the pause.
4. The Job was already `running` and was enqueued with `safe_to_retry_after_crash=False`, `max_attempts=1`. After lease expiry, recovery marks the Job and Operation `uncertain`; it does not requeue or replay the write.
5. Trigger reconciliation. Adapter read-back confirms that no Campaign was created, so the original Operation and provisioning resolve to `failed`.
6. Verify no duplicate objects and exactly one dispatched write intent.
7. Assert via `FakeMetaObjectRecord`, `MetaCallIntentRecord`, Job state, and Operation state inspection.

### Scenario 9: Definitive parity failure and terminal provisioning

Issue #88's retry/reconciliation evidence is split by the dispatch marker: Scenario 5b proves safe retry of an expired `leased` but never-dispatched Job; Scenarios 4 and 5c prove reconciliation without replay after dispatch. This scenario separately proves a definitive terminal failure.

An exception raised by `FakeMetaAdapter.ensure_object()` occurs after the durable dispatch marker and is therefore uncertainty, not a definitive failure. To exercise the actual failure path, the controlled-mock adapter returns a normal successful outcome whose stored AdSet state has a deterministic budget mismatch while remaining `PAUSED`. The saga completes its read-backs and the post-provisioning parity check fails. Before raising `ProvisioningError`, `execute_provisioning()` must durably commit the failed parity record and the provisioning transition to `failed`; otherwise the surrounding transaction would roll both back. The Worker then persists terminal failed Operation and Job states.

Uses a `controlled_mock` Launch path.

1. Freeze and confirm Launch under `controlled_mock`. Seed `return_state_patch` for one AdSet so its stored `daily_budget_minor` differs from the frozen preview while `status` remains `PAUSED`.
2. Saga dispatches each write and receives definitive successful outcomes. All created objects remain PAUSED.
3. Read-back observes the injected budget mismatch. The post-provisioning parity record is written with `budget_parity_verified=false` and `passed=false`.
4. `execute_provisioning()` transitions provisioning to `failed`, durably commits the failed parity evidence/state, and then raises `ProvisioningError`.
5. The Worker transitions the Operation to `failed` and marks the Job failed with `retryable=False`. The Launch Job was created with `safe_to_retry_after_crash=False`, `max_attempts=1`, so no automatic retry occurs.
6. Verify the specific parity evidence and error are visible through persisted Operation/provisioning reads.
7. Verify every created object is still PAUSED and no duplicate or unsupported write was produced.

**Explicitly out of scope for this issue:**
- There is no retry command for terminal `failed` or `partially_provisioned` Operation states. The current system does not support manual retry from these states, and this design does not invent one.
- A follow-up issue should address whether `partially_provisioned` and terminal `failed` states need a fenced retry/continuation mechanism.
- The test verifies that terminal states are genuinely terminal and that failures are surfaced with actionable detail. Issue #88's safe retry/reconciliation evidence remains covered by Scenarios 5b, 4, and 5c as described above.

### Scenario 6: Authorization — Member/Owner and cross-Workspace

1. Member can create, analyze, resolve, freeze, and run experiments in their workspace.
2. Owner controls enforced where required: the existing model grants both Owner and Member access to all experiment operations (create, analyze, freeze, launch, judge, confirm). Authorization boundaries are at the Workspace level (cross-Workspace rejection), not Owner-vs-Member for experiments. This scenario verifies that both roles can complete the full lifecycle in their own workspace.
3. Cross-Workspace access attempt — verify server rejection without entry leakage (analysis and freeze both return 404).
4. Foreign workspace identifier in request — rejected server-side.

### Scenario 7: Mode contamination

1. Golden-path Launch (Scenario 1) uses `customer_sandbox` — verify all FakeMetaAdapter evidence is tagged with `customer_sandbox` evidence mode. No fault injection occurs in this path.
2. Fault-injected Launches (Scenarios 4, 5, 9) use `controlled_mock` — verify all evidence from those paths is tagged `controlled_mock`.
3. Negative test: a Launch request specifying `controlled_mock` or `internal_validation` mode cannot cross the `customer_sandbox` evidence boundary. A request specifying `customer_sandbox` cannot cross the `controlled_mock` boundary.
4. Verify evidence mode badge visible and accurate in the UI for both modes.
5. `controlled_mock` evidence is decision-ineligible: judgement/confirmation must not create a Winner from it, so there is no controlled-mock Winner available for a `customer_sandbox` experiment to reference.
6. `internal_validation` evidence from the platform validation workspace cannot contaminate any customer workspace.

### Scenario 8: No-duplicate freeze (negative duplicate path)

1. Create experiment with entries that have distinct concept keys.
2. Analysis returns `has_duplicates: false`, empty groups.
3. Freeze succeeds with empty `concept_resolution_plan` and valid `analyzed_candidate_set_hash`.
4. Frozen evidence shows `had_duplicates: false`, `resolutions: []`.

## §3.3 Persistence assertions (applied across scenarios)

- Browser refresh only after durable milestones: after freeze, after provisioning, after experiment started, after judgement, after confirmation. Reloading after preview confirmation but before execution intentionally loses the short-lived confirmation token — do not assert refresh survival there.
- API restart — persisted reads recover while the Worker continues independently; the API does not resume the saga.
- Worker restart after lease but before `mark_running()`/dispatch — the expired `leased` Job is safely requeued, claimed by the replacement Worker, and executes once.
- Worker restart after the durable dispatch marker — the non-retryable running Job becomes uncertain after lease expiry and requires reconciliation; it is never blindly requeued.
- No duplicate FakeMetaAdapter objects after any restart.

## §3.4 PAUSED and write-safety assertions

- Every FakeMetaAdapter-created object has PAUSED status — asserted via persisted parity check plus `FakeMetaObjectRecord` inspection (read from database, not browser network intercept).
- `MetaCallIntentRecord` inspection — no unsupported write operations recorded.
- Passive browser request assertions: UI only used allowlisted BFF routes (separate from adapter-level assertions).

## §3.5 Closed-state evidence

The current v1 interface/UI has no `close` command. Testing identical evidence in closed state uses a hybrid fixture approach that respects the test-seam rule:

1. **Produce evidence through production behavior**: the fixture creates a real experiment, freezes it through the real `freeze_experiment_v2` service (with or without concept resolution as needed), producing genuine `ExperimentVersionResolutionRecord` rows and `concept_resolutions_hash`.
2. **Transition only the unreachable state**: the fixture then directly sets the experiment state to `closed` via a test-only database update — this is the only manufactured outcome, and it is the state transition itself (which is otherwise unreachable without a close command), not the resolution evidence.
3. **Display test**: the test verifies the evidence section renders identically to frozen/running states, using the same `FrozenResolutionEvidence` component.

This ensures resolution evidence is never database-injected — it comes from the real freeze service. Only the closed state (which the UI cannot currently produce) is manufactured.

If adding a close command is desired, it should be a separate scope decision.

## §3.6 Suite organization

- `web/e2e/experiments/duplicate-resolution.spec.ts` — Scenarios 1, 2, 8 (resolution and freeze lifecycle). Scenario 1 is the `customer_sandbox` golden path with no fault injection.
- `web/e2e/experiments/launch-recovery.spec.ts` — Scenarios 4, 5, 9 (uncertain dispatch, reconciliation, restart recovery, definitive parity failure). Uses `controlled_mock` evidence mode with fault injection. One Playwright worker for container-restart scenarios (5a–5c).
- `web/e2e/experiments/judgement-winner.spec.ts` — Scenario 3 (inconclusive) and golden-path judgement/confirmation continuation from Scenario 1.
- `web/e2e/experiments/authorization-mode.spec.ts` — Scenarios 6, 7.
- `web/e2e/experiments/closed-state-evidence.spec.ts` — §3.5 display test (evidence produced by real freeze, only closed state manufactured).
- `web/e2e/experiments/fixtures.ts` — replaced: old route-mock content removed, new real-backend fixtures (workspace provisioning, authenticated context, API inspection helpers).
- `web/e2e/experiments/helpers.ts` — new: fault injection helpers, restart helpers, `FakeMetaObjectRecord`/`MetaCallIntentRecord` inspection.
- `web/e2e/experiments/worker_checkpoint.py` — new: guarded one-shot claimant for Scenario 5b; leases one exact expected Job through `jobs.claim_next()` and exits before `mark_running()`.

Old route-mocked `golden-path.spec.ts` is deleted. Isolated component tests using browser-only mocks may remain if useful but are not acceptance evidence.

---

# Section 4: Files Summary

## New files

| File | Purpose |
|------|---------|
| `web/components/experiments/DuplicateResolutionPanel.tsx` | Resolution UI in main column |
| `web/components/experiments/FrozenResolutionEvidence.tsx` | Post-freeze evidence display |
| `web/e2e/experiments/helpers.ts` | E2E restart helpers, fault injection, `FakeMetaObjectRecord`/`MetaCallIntentRecord` inspection |
| `web/e2e/experiments/seed_prerequisites.py` | Guarded PostgreSQL fixture setup, including immutable controlled-mock scenarios |
| `web/e2e/experiments/worker_checkpoint.py` | Guarded one-shot real queue claimant for the leased-before-running crash checkpoint |
| `web/e2e/experiments/duplicate-resolution.spec.ts` | Scenarios 1, 2, 8 (`customer_sandbox` golden path) |
| `web/e2e/experiments/launch-recovery.spec.ts` | Scenarios 4, 5, 9 (`controlled_mock` fault paths) |
| `web/e2e/experiments/judgement-winner.spec.ts` | Scenario 3, judgement/confirmation |
| `web/e2e/experiments/authorization-mode.spec.ts` | Scenarios 6, 7 |
| `web/e2e/experiments/closed-state-evidence.spec.ts` | §3.5 display test |

## Modified files

| File | Change |
|------|--------|
| `app/experiments/service.py` | Extract shared candidate loader + detector; add `analyze_duplicates()`; populate evidence on detail; three-way runtime hash invariant |
| `app/experiments/v1_router.py` | Add `GET /{experiment_id}/duplicate-analysis` (private domain route) |
| `app/experiments/response_models.py` | Add analysis response models, evidence models on ExperimentDetail |
| `app/meta/connections.py` | Allow the internal connection-construction interface to receive the immutable initial scenario configuration |
| `app/adapters/meta/fake_adapter.py` | Consume existing `_fault_config` during `ensure_object()` |
| `app/launch/api.py` | Admit mode-matched `controlled_mock` previews through the existing execute route; keep `internal_validation` forbidden |
| `app/launch/recovery.py` | Reconstruct parity read-only after a complete reconciled write manifest |
| `app/launch/saga.py` | Durably persist failed parity evidence and terminal provisioning state before raising `ProvisioningError` |
| `web/components/experiments/ExperimentsWorkspace.tsx` | Integrate resolution panel, wire freeze submission, add evidence display, error handling, controlled-mock mode selector |
| `web/hooks/experiments/useExperimentsWorkspace.ts` | Add `analyzeDuplicates`, update freeze, and pass selected Meta mode through preview/provisioning reads |
| DomainGateway + `useDomainQuery` switch files | Register new GET route |
| BFF route handler | Add BFF mapping for `duplicate-analysis` GET route |
| OpenAPI spec | Regenerated |
| Generated TypeScript client | Regenerated |
| BFF allowlist | Updated for new route |
| `web/e2e/experiments/fixtures.ts` | Replaced: route-mock content removed, new real-backend fixtures |
| `tests/test_fake_meta_adapter.py` | Deterministic fault-effect coverage |
| `tests/test_launch_recovery.py` | Complete-manifest read-only parity reconstruction and terminal partial outcomes |
| `tests/test_launch_saga.py` | Definitive parity-failure coverage with PAUSED objects |

## Deleted files

| File | Reason |
|------|--------|
| `web/e2e/experiments/golden-path.spec.ts` | Replaced by real-backend test suite |
