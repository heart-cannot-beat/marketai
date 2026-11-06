# Issue #48: New Generation and Run Recovery Workspace UI — Architecture Specification

**Date:** 2026-09-03
**Issue:** [#48 — Build the New Generation and Run recovery workspace UI](https://github.com/heart-cannot-beat/marketai-archive/issues/48)
**Status:** Approved
**Blocked by:** #50, #51, #52, #53, #54
**Part of:** #27 (Build New Generation, Assets, and Creative Library UI)
**Depends on:** PR #45 (Generation → Assets vertical slice, merged), PR #46 (BFF Bridge, merged)

## Overview

This specification defines the routing architecture, state model, component structure, and delivery decomposition for the New Generation workspace UI. It replaces the current `FeatureEmptyState` stub on `/w/{slug}/generate` with a complete composition, preview, submission, progress, and recovery workflow backed by the production Domain Interface delivered in PR #45 and exposed through the BFF Bridge in PR #46.

The design spans six sections:

1. **Routing architecture** — nested sub-routes, page shells, navigation, deep-link and refresh invariants.
2. **State model and data flow** — server-truth principle, SWR reads, command lifecycle, intent persistence, operation tracking, and the `RunProgressSource` abstraction.
3. **Component architecture** — feature modules, shared primitives, module boundaries, and file organization.
4. **Delivery slices** — three sequential slices with scope, prerequisites, file inventory, and acceptance criteria.
5. **Cross-cutting concerns** — authorization, execution-mode provenance, accessibility, error states, and testing strategy.
6. **Hard prerequisites and acceptance matrix** — backend blockers, interface prerequisites, per-slice gates, and the full verification matrix.

---

# Section 1: Routing Architecture

## Route table

| Route | Page file | Purpose |
|---|---|---|
| `/w/{slug}/generate` | `pages/w/[slug]/generate/index.tsx` | Draft and Run list, new Draft entry point |
| `/w/{slug}/generate/drafts/[draftId]` | `pages/w/[slug]/generate/drafts/[draftId].tsx` | Draft composition + Resolution Preview |
| `/w/{slug}/generate/runs/[runId]` | `pages/w/[slug]/generate/runs/[runId].tsx` | Run progress, RunItem detail, Recovery |

**Route migration**: The existing `pages/w/[slug]/generate.tsx` must be deleted when `pages/w/[slug]/generate/index.tsx` is created. Next.js resolves both to `/w/{slug}/generate`; keeping both creates a build conflict.

## Sub-route query parameters

- **Draft page**: `?preview={previewId}` — selects the current Preview for display. Absence means the Draft is in editing mode. The Preview is subordinate to its Draft and does not warrant a separate route.
- **Run page**: `?item={runItemId}` — selects a RunItem for detail display within the progress view. `?command={operationId}` — tracks an in-flight recovery command Operation. These are subordinate to the Run.

Note: Recovery previews are **not** persisted in the URL. `GenerationRecoveryPreview` has no server-assigned `id`, no `preview_hash`, and no `GET`-by-ID endpoint — the response is transient. The recovery flow holds the preview in component state; a refresh returns the user to the Run observation view where they can re-request a recovery preview. See the Recovery design gap in Section 6.

## Page shell pattern

Every page file is a thin route shell. It:

1. Defines a `NextPageWithLayout<Props>` component.
2. Attaches `getLayout` wrapping `WorkspaceLayout`.
3. Exports `getServerSideProps = getWorkspacePageProps()`.
4. Renders one root feature component from the shared feature modules.
5. Passes subordinate components (preview panel, submit confirmation, recovery panel) as composition slots — the shell is the composition point, not a feature module.

The page shell never contains domain logic, state management, or layout beyond the root feature component and slot wiring. This avoids the anti-pattern of splitting one giant page into three giant pages.

```tsx
// Example: pages/w/[slug]/generate/drafts/[draftId].tsx
const DraftPage: NextPageWithLayout<Props> = () => {
  const router = useRouter();
  const draftId = router.query.draftId as string;
  const previewId = (router.query.preview as string) ?? null;

  return (
    <DraftWorkspace draftId={draftId} previewId={previewId}>
      {/* Composition slot: preview/submit wired by the page shell, not
          imported laterally by the draft module */}
      {previewId && <PreviewPanel previewId={previewId} />}
    </DraftWorkspace>
  );
};

DraftPage.getLayout = function getLayout(page: ReactElement) {
  const { bootstrap, errorCode, errorContext } = page.props as Props;
  return (
    <WorkspaceLayout bootstrap={bootstrap} errorCode={errorCode} errorContext={errorContext}>
      {page}
    </WorkspaceLayout>
  );
};

export const getServerSideProps = getWorkspacePageProps();
export default DraftPage;
```

## Navigation active state

The current `WorkspaceNavigation` uses exact pathname matching:

```ts
const isActive = (area: string) => activePathname === `/w/${slug}/${area}`;
```

This must change to prefix matching for the `generate` area so that `/w/{slug}/generate/drafts/xxx` and `/w/{slug}/generate/runs/xxx` keep "New Generation" highlighted:

```ts
const isActive = (area: string) =>
  activePathname === `/w/${slug}/${area}` ||
  activePathname?.startsWith(`/w/${slug}/${area}/`) === true;
```

Other workspace areas (`assets`, `experiments`, etc.) continue to use their current flat routes and are unaffected by this change. If they later adopt sub-routes, the same prefix logic applies.

## Route transitions

| Trigger | From | To |
|---|---|---|
| User clicks "New Draft" | List page | Setup dialog collects required fields → `create_generation_draft` → on success, `router.push` to `/generate/drafts/{newDraftId}` |
| User clicks existing Draft in list | List page | `router.push` to `/generate/drafts/{draftId}` |
| User clicks existing Run in list | List page | `router.push` to `/generate/runs/{runId}` |
| Preview created | Draft page | `router.replace` adding `?preview={previewId}` (same page, no navigation) |
| Run submitted successfully | Draft page | Submit returns `OperationRef` with `generation_run_id`; `router.push` to `/generate/runs/{generation_run_id}` immediately. The Run page then polls the Operation through the Run's `commands[]` list |
| User navigates back from Draft/Run | Draft or Run page | Browser back → list page or previous Draft/Run |

`router.push` is used for forward navigation (adds history entry). `router.replace` is used for subordinate state changes (preview selection) that should not create new history entries.

## URL identity invariant

URL IDs (`draftId`, `runId`, `previewId`) are resource selectors only. Workspace identity is derived exclusively from the `{slug}` path segment, which `getWorkspacePageProps` resolves server-side to an authenticated `WorkspaceBootstrap`. The BFF proxy resolves slug → `workspace_id` in its 13-step pipeline. No URL parameter is trusted as a tenant boundary.

## Deep-link and refresh invariants

These are acceptance-test requirements, not guidelines:

1. **Direct navigation** to any route (copy-paste URL, bookmark, shared link) must authenticate, resolve workspace membership, and render the correct resource from server state.
2. **Browser refresh** (F5) on any route must produce the same view as initial navigation, because all business state comes from server queries, not client memory.
3. **Browser back/forward** must navigate between routes correctly. Subordinate state changes (`?preview=`) must not pollute the history stack (use `router.replace`).
4. **Cross-workspace URL**: Resource IDs belonging to a different workspace must return **404** (enforced by RLS — the row is invisible). **403** is reserved exclusively for workspace membership denial (the user has no membership in the workspace identified by the slug). The UI never leaks a foreign resource's existence.

---

# Section 2: State Model and Data Flow

## Server-truth principle

All business state lives on the server. The browser's role is cache-and-display, never compute-and-decide. This directly implements the v9 contract §2.1: "浏览器状态只能用于缓存和展示，不能决定业务真值。"

| State source | Mechanism | Persistence | Purpose |
|---|---|---|---|
| Domain entity reads | `useDomainQuery` → SWR | In-memory cache | Draft, Preview, Run, RunItem, Asset display |
| Domain mutations | `useDomainCommand` | Three-way result: success / problem / reconciliation | Create/update Draft, create Preview, submit Run, recovery commands |
| Operation lifecycle | `useOperation` | `sessionStorage` (operation ID) | Tracks async command completion; polls until terminal or uncertain-resolved |
| Idempotency intents | `IntentTracker` | `localStorage` (24h safe-retry, 48h retention) | Prevents double-submit; retained ambiguous intents surface `ReconciliationRequired` past safe-retry window and persist until explicitly resolved or dismissed |
| Run progress | `useRunProgress` (new) | In-memory | Adaptive polling for Run/RunItem state transitions |
| Capabilities | `useDomainQuery('get_generation_capabilities')` | SWR cache | Provider/mode/format constraints |
| Library entities | `useDomainQuery` (library reads) | SWR cache | Product, Person, Template, Reference selection |

No `useState`, `useReducer`, or context provider holds authoritative domain state. Form state within the Draft composer is local React state that is flushed to the server via `update_generation_draft` (CAS) on blur/save.

## Conditional-header support prerequisite

The current `DomainGateway.sendSafe()` does not send `If-None-Match` for GET requests (only `pollOperation` does via a separate code path). The current `DomainGateway.sendMutating()` / `runCommand()` does not accept or forward `If-Match` headers for CAS updates.

Before Slice 1 can implement Draft CAS editing, the gateway and hooks must be extended:

1. **`sendSafe`**: Accept an optional `ifNoneMatch` parameter for ETag-validated reads.
2. **`sendMutating` / `runCommand`**: Accept an optional `ifMatch` parameter, forwarded as the `If-Match` header. Required for `update_generation_draft` (PATCH), `create_generation_preview` (POST with implicit draft revision check), and all recovery commands that reference a Run revision.
3. **`useDomainCommand`**: Accept `ifMatch` in `ExecuteParams` and pass it through to the gateway.
4. **BFF proxy**: The proxy `forwardConditionalHeaders` policy already exists in `marketai-domain-v1-policy.json` — verify it covers `If-Match` forwarding for the affected operations.

This is listed as a hard prerequisite in Section 6.

## Data flow per phase

### Phase 1: Draft Creation + Composition

**New Draft staging**: `CreateDraftInput` requires `product_id`, `provider`, `model`, and `execution_mode` at creation time. The list page presents a setup dialog or inline form that collects these required fields before calling `create_generation_draft`. The Capabilities endpoint provides the valid options. On success, the user is navigated to the Draft page where they refine selectors, dimensions, references, and exclusions via CAS updates.

```
User clicks "New Draft"
        │
        ▼
  Setup dialog (product, provider, model, mode)
  ← Capabilities query provides valid options
        │
  user confirms
        │
        ▼
  create_generation_draft (POST)
        │
  success → router.push → /drafts/{id}
```

**Draft editing** uses the **hybrid data sourcing** model:

```
Capabilities query ──┐
Library queries ─────┤
                     ├──► DraftComposer form state
                     │         │
                     │    blur/save
                     │         │
                     │         ▼
                     │    update_generation_draft (PATCH, If-Match: revision ETag)
                     │         │
                     │    success / 412 Precondition Failed (conflict)
                     │         │
                     ▼         ▼
              get_generation_draft (SWR revalidate)
```

- **Capabilities endpoint** (#50) provides: available providers, models, execution modes, format options, entitlement constraints, and dimension limits. This drives the configuration controls.
- **Library read endpoints** provide: published ProductVersions, OfferVersions, PersonVersions, ReferenceAssetVersions, DimensionTemplateVersions, and PromptPolicyVersions. Each picker shows exact versions, not latest-mutable entries. The Draft stores explicit version references in `selectors`.

Capabilities are fetched once per Draft page load (SWR-cached). Library reads are fetched on demand as the user opens pickers.

### Phase 2: Preview + Submission

```
create_generation_preview ──► GenerationPreviewDetail (frozen)
        │                        │
   ?preview={id}            stale check
   (router.replace)         (TTL + hash)
                                 │
                            user confirms
                                 │
                                 ▼
                    submit_generation_run
                    (preview_id + preview_hash)
                                 │
                                 ▼
                          OperationRef
                    { operation_id, generation_run_id }
                                 │
                    router.push → /runs/{generation_run_id}
                                 │
                    Run page reads Run.commands[] to find
                    the submission Operation and poll it
```

**Submit recovery across refresh**: Rather than relying on `sessionStorage` to recover an Operation ID (which `useOperation` writes but does not auto-read), the submit flow navigates immediately using `generation_run_id` from the `OperationRef` response. After refresh, the Run page loads `GenerationRunDetail`, reads its `commands[]` list, finds the latest non-terminal command Operation, and resumes polling it via `useOperation`. This means:

- No `sessionStorage`-based Operation recovery is needed for submit.
- The Run page is the single recovery surface for both initial submission and recovery commands.
- If the browser crashes between receiving `OperationRef` and completing navigation, the user returns to the Draft page; the Draft's `allowed_actions` will reflect the existing Run (no re-submit is possible for the same preview).

Stale-preview protection: the Preview has a `valid_until` timestamp and a `preview_hash`. The UI must:
1. Display a countdown or "valid for X minutes" indicator.
2. Disable the submit button when `valid_until` has passed.
3. On submit, send both `preview_id` and `preview_hash`; the server rejects mismatches.
4. If the preview is invalidated (Draft edited, library dependency changed), show an explicit "Preview outdated — regenerate" message. The `invalidation_reason` field on `GenerationPreviewDetail` carries the server's explanation.

### Phase 3: Run Progress

```
get_generation_run ─────────────┐
list_generation_run_items ──────┤ (visible page only)
                                ├──► useRunProgress
                                │    (adaptive polling)
get_operation (latest command)──┘
                                │
                                ▼
                          RunProgressView
                          ├── RunSummary (status, done/total/failed)
                          ├── RunItemGrid (paginated, state badges)
                          ├── RunItemDetail (selected item)
                          └── OperationTracker (if command Operation non-terminal)
```

### Phase 4: Recovery

```
allowed_actions (from GenerationRunDetail)
        │
        ▼
  RecoveryPanel
  (server-driven action buttons)
        │
  user selects action
        │
        ▼
  create_generation_run_recovery_preview (for retry/resume)
  (kind + targets)
        │
        ▼
  RecoveryPreview display (held in component state — transient, not URL-persisted)
  (targets, estimated_additional_cost, valid_until)
        │
  user confirms
        │
        ▼
  retry_failed_items / resume_paused_items / reconcile_run / cancel_run
  (preview_id + preview_hash where applicable)
        │
        ▼
  RunCommandResult { operation_id, run_id }
        │
  useOperation → poll until terminal
        │
  on completion → revalidate Run + RunItem queries
```

## `RunProgressSource` abstraction

Run progress is wrapped behind a replaceable adapter interface so the current polling implementation can be replaced with SSE without changing any component API.

```ts
interface RunProgressSnapshot {
  run: GenerationRunDetail;
  items: GenerationRunItemDetail[];     // Current visible page only
  itemsPagination: { cursor: string | null; hasMore: boolean };
  latestCommandOperation: OperationDetailResponse | null;
  isRunTerminal: boolean;               // Run.status in terminal set
  isOperationTerminal: boolean;         // Latest command Operation in terminal set (or null)
  isPartialSuccess: boolean;
  lastRefreshedAt: number;
  transportError: Error | null;
}

interface RunProgressSource {
  subscribe(runId: string, options: { itemPageCursor?: string }, onSnapshot: (s: RunProgressSnapshot) => void): void;
  unsubscribe(): void;
  forceRefresh(): void;
  setItemPage(cursor: string | null): void;   // Change which page of RunItems to poll
}
```

The initial implementation is `PollingRunProgressSource`.

**Two-level terminal distinction**: Operation `uncertain` and Run `uncertain` have different polling semantics:

- **Operation `uncertain`**: The Operation's outcome is ambiguous (e.g., worker crash during dispatch). `useOperation` does NOT treat this as terminal — it continues polling, because the reconciler or worker may resolve it to a definitive state (`running`, `succeeded`, `failed`). The `PollingRunProgressSource` mirrors this: if the latest command Operation is `uncertain`, polling continues at 10–30s with jitter.
- **Run status `uncertain`**: Derived from RunItem states — some items are `uncertain` after a crash. The Run itself may not have a non-terminal Operation. In this case, `isRunTerminal` is false (the Run is not in a terminal business state), but `isOperationTerminal` may be true (the submission Operation completed; it's the Run that needs reconciliation). Polling stops only when both the Run and the latest command Operation are terminal.

| State | Poll interval | Behavior |
|---|---|---|
| Operation `queued` | 10–30s with jitter | Waiting for worker to claim |
| Operation `running` | 1–2s | Fast updates for live progress |
| Operation `uncertain` | 10–30s with jitter | Continue polling — reconciler may resolve |
| Operation terminal + Run non-terminal | 5–10s | Run may still be progressing from prior command |
| Both terminal | Stopped | No further polling |

Additional behaviors:
- **Visibility recovery**: On `document.visibilitychange` (tab becomes visible) or `window.focus`, immediately fire one refresh regardless of interval. This behavior lives exclusively in `PollingRunProgressSource` — `useRunProgress` delegates to `forceRefresh()` and does not register its own visibility listener.
- **Transport failure**: A failed poll increments a `consecutiveFailures` counter with exponential backoff (max 60s). The `transportError` field is populated so the UI can display "Connection issue — retrying" distinct from Operation business failures. A transport failure never creates a terminal state or a new intent.
- **Four-way state distinction**: (1) `transportError !== null` — poll failed, retrying; (2) Operation `uncertain` — ambiguous, still polling, may need reconciliation; (3) Run `uncertain` items — reconciliation needed; (4) Operation/Run `failed` — terminal failure with error summary. These are four separate display states.
- **Pagination-aware polling**: `RunProgressSnapshot.items` contains only the currently visible page of RunItems (cursor-paginated, default 50). The adapter polls the visible page, not all items. `RunSummary` counters (`done`, `total`, `failed`) come from `GenerationRunDetail` and are always complete. The user navigates pages via `setItemPage()`, which triggers a fresh fetch of that page.

The `useRunProgress` hook wraps `RunProgressSource`:

```ts
function useRunProgress(runId: string): RunProgressSnapshot & {
  forceRefresh: () => void;
  setItemPage: (cursor: string | null) => void;
};
```

It creates a `PollingRunProgressSource` on mount, subscribes, and returns the latest snapshot as React state. It unsubscribes on unmount. Future SSE replaces only the `RunProgressSource` implementation.

---

# Section 3: Component Architecture

## File organization

```
web/
├── pages/w/[slug]/generate/
│   ├── index.tsx                    # List page shell
│   ├── drafts/
│   │   └── [draftId].tsx            # Draft page shell (composes preview slot)
│   └── runs/
│       └── [runId].tsx              # Run page shell (composes recovery slot)
│
├── components/generation/
│   ├── shared/
│   │   ├── RunProgressSource.ts     # Interface + PollingRunProgressSource
│   │   ├── useRunProgress.ts        # React hook wrapping RunProgressSource
│   │   ├── useGenerationNav.ts      # Route helpers (buildDraftUrl, buildRunUrl, etc.)
│   │   ├── RunItemStateBadge.tsx    # RunItem state → badge color/label
│   │   ├── CostDisplay.tsx          # Decimal + currency formatted display
│   │   └── ExecutionModeTag.tsx     # Wraps ModeProvenance for generation context
│   │
│   ├── list/
│   │   ├── GenerationListView.tsx   # Root component for list page
│   │   ├── DraftList.tsx            # Draft summaries with status, actions
│   │   ├── RunList.tsx              # Run summaries with status, progress counts
│   │   └── NewDraftDialog.tsx       # Setup dialog: product, provider, model, mode → create
│   │
│   ├── draft/
│   │   ├── DraftWorkspace.tsx       # Root component for draft page; accepts children slot
│   │   ├── DraftComposer.tsx        # Orchestrates library selection + config
│   │   ├── ProductSelector.tsx      # Library ProductVersion picker (exact version)
│   │   ├── DimensionConfigurator.tsx # DimensionTemplateVersion + value configuration
│   │   ├── CombinationStrategy.tsx  # Full combination vs sampled with cap
│   │   ├── ProviderConfig.tsx       # Provider, model, execution mode, Seed
│   │   ├── FormatConfig.tsx         # Dimensions, aspect ratio, resolution, variants
│   │   ├── ReferenceSelector.tsx    # ReferenceAssetVersion picker with bindings
│   │   ├── ExclusionConfig.tsx      # Loser-combination skip, invalid/contradictory flagging, manual exclusions
│   │   └── DraftSaveIndicator.tsx   # CAS save status (saving, saved, conflict)
│   │
│   ├── preview/
│   │   ├── PreviewPanel.tsx         # Full frozen preview display
│   │   ├── PreviewValidity.tsx      # TTL countdown + stale indicator
│   │   ├── PromptInspector.tsx      # Final assembled prompt (read-only, auditable)
│   │   ├── CombinationSummary.tsx   # Combination count, call count, items breakdown
│   │   ├── CostEstimate.tsx         # Max cost with currency and guardrail result
│   │   ├── CapabilityDecisions.tsx  # Provider capability resolution display
│   │   └── SubmitConfirmation.tsx   # Confirm button with stale-preview guard
│   │
│   ├── progress/
│   │   ├── RunProgressView.tsx      # Root component for run page; accepts recoverySlot
│   │   ├── RunSummary.tsx           # Overall status, done/total/failed counters
│   │   ├── RunItemGrid.tsx          # Paginated RunItem grid with state badges
│   │   ├── RunItemDetail.tsx        # Selected item detail (state, error, execution mode)
│   │   └── OperationTracker.tsx     # Inline Operation status for submit/recovery commands
│   │
│   └── recovery/
│       ├── RecoveryPanel.tsx        # Server-driven actions from allowed_actions
│       ├── RecoveryPreviewDisplay.tsx # Targets, additional cost, validity (transient state)
│       ├── ReconciliationGuidance.tsx # Uncertain items explanation and action
│       └── CancelConfirmation.tsx   # Cancel with in-flight warning
```

## Module boundaries and composition slots

Each module folder owns its root component and internal components. Dependencies flow downward:

```
pages (shells) → components/generation/{module}
                       ↓
              components/generation/shared
                       ↓
              components/workspace/{OperationStatus, ModeProvenance, StaleIndicator, ...}
                       ↓
              hooks/domain/{useDomainQuery, useDomainCommand, useOperation}
                       ↓
              lib/domain-client/{gateway, intent, csrf, result-types}
```

**No lateral imports between feature modules.** The `draft` module does not import from `preview` or `progress`. The `progress` module does not import from `recovery`. Cross-module composition is handled by **page shells as composition points**:

- The Draft page shell imports both `DraftWorkspace` (from `draft`) and `PreviewPanel` / `SubmitConfirmation` (from `preview`) and passes the preview components as `children` into `DraftWorkspace`'s slot.
- The Run page shell imports both `RunProgressView` (from `progress`) and `RecoveryPanel` (from `recovery`) and passes `RecoveryPanel` as a `recoverySlot` render prop.

This keeps each module independently testable and avoids circular or lateral dependencies.

## Server-driven UI principle

The UI never computes action eligibility client-side. Every detail response includes `allowed_actions: ActionEligibility[]`, where each action has:

```ts
{ action: GenerationAction; allowed: boolean; reason_code: string | null }
```

`GenerationAction` is an enum whose values are defined in the OpenAPI contract and the BFF route-matrix (`marketai-domain-v1-policy.json`). The UI maps action enum values to button/control rendering:

- If `allowed` is true, the button is enabled.
- If `allowed` is false, the button is disabled with `reason_code` resolved to a human-readable i18n message.
- If an action is absent from `allowed_actions`, its control is not rendered at all.

This means:
- Recovery actions (retry, resume, cancel, reconcile) appear only when the server declares them eligible.
- The UI does not replicate the server's eligibility logic (role checks, state checks, entitlement checks).

---

# Section 4: Delivery Slices

## Slice 1: Draft + Preview

**Scope:** Replace `FeatureEmptyState` with a working Draft list, Draft creation dialog, Draft composition, and Resolution Preview display.

**Hard prerequisites:**
- #50 closed (Generation capabilities endpoint returns real data)
- #52 closed (Enriched `GenerationPreviewDetail` with combination count, call count, cost, prompt, items, capability decisions, exclusions)
- #54 closed (Conditional-header gateway support for `If-Match` / `If-None-Match`)
- PR #45 and PR #46 merged (already true)

**Files deleted:**

| File | Reason |
|---|---|
| `pages/w/[slug]/generate.tsx` | Replaced by `pages/w/[slug]/generate/index.tsx` |

**Files created:**

| File | Type |
|---|---|
| `pages/w/[slug]/generate/index.tsx` | New list page shell |
| `pages/w/[slug]/generate/drafts/[draftId].tsx` | New page shell |
| `components/generation/shared/useGenerationNav.ts` | New |
| `components/generation/shared/CostDisplay.tsx` | New |
| `components/generation/shared/ExecutionModeTag.tsx` | New |
| `components/generation/list/GenerationListView.tsx` | New |
| `components/generation/list/DraftList.tsx` | New |
| `components/generation/list/NewDraftDialog.tsx` | New |
| `components/generation/draft/DraftWorkspace.tsx` | New |
| `components/generation/draft/DraftComposer.tsx` | New |
| `components/generation/draft/ProductSelector.tsx` | New |
| `components/generation/draft/DimensionConfigurator.tsx` | New |
| `components/generation/draft/CombinationStrategy.tsx` | New |
| `components/generation/draft/ProviderConfig.tsx` | New |
| `components/generation/draft/FormatConfig.tsx` | New |
| `components/generation/draft/ReferenceSelector.tsx` | New |
| `components/generation/draft/ExclusionConfig.tsx` | New |
| `components/generation/draft/DraftSaveIndicator.tsx` | New |
| `components/generation/preview/PreviewPanel.tsx` | New |
| `components/generation/preview/PreviewValidity.tsx` | New |
| `components/generation/preview/PromptInspector.tsx` | New |
| `components/generation/preview/CombinationSummary.tsx` | New |
| `components/generation/preview/CostEstimate.tsx` | New |
| `components/generation/preview/CapabilityDecisions.tsx` | New |

**Files modified:**

| File | Change |
|---|---|
| `components/workspace/WorkspaceNavigation.tsx` | `isActive` → prefix matching for `generate` |

**Scope boundary:** This slice does not include the Submit button, Run progress, or Recovery. The Preview is display-only — it shows the frozen resolution but does not yet wire `submit_generation_run`. The list page shows Drafts only (no Runs column yet). `SubmitConfirmation` is deferred to Slice 2. Regeneration intents (`same_recipe`, `new_seed`, `edit_prompt`, `quality_feedback`) are excluded — they belong to the Asset regeneration flow tracked by #49, and `CreateDraftInput` always uses `new_generation` for direct draft creation.

**Acceptance criteria:**

| # | Criterion | Test type |
|---|---|---|
| 1.1 | "New Draft" opens setup dialog; submitting creates a Draft and navigates to `/generate/drafts/{id}` | Playwright |
| 1.2 | Setup dialog loads Capabilities to populate provider, model, mode, and product options | Jest (hooks) + Playwright |
| 1.3 | Draft composition selects exact published versions: ProductVersion, OfferVersion, PersonVersion, ReferenceAssetVersion, DimensionTemplateVersion, PromptPolicyVersion | Playwright |
| 1.4 | Seed configuration: user can set explicit Seed or leave auto; Draft stores the choice | Playwright |
| 1.5 | Draft form saves via CAS (`update_generation_draft` with `If-Match`); 412 conflict shows "Draft was modified — reload" recovery | Jest (hook) + Playwright |
| 1.6 | "Generate Preview" creates a Preview and adds `?preview={id}` to URL | Playwright |
| 1.7 | Preview displays: combination count, call count, final assembled prompt, exclusions, per-item references, capability decisions, execution mode, and maximum cost (Decimal + currency) | Playwright |
| 1.8 | Stale preview (past `valid_until` or `invalidation_reason` set) disables confirm area and shows reason | Jest (component) |
| 1.9 | Browser refresh on `/generate/drafts/{id}?preview={pid}` restores Draft + Preview from server | Playwright |
| 1.10 | Browser refresh on `/generate` restores Draft list from server | Playwright |
| 1.11 | Deep link to `/generate/drafts/{id}` with foreign resource ID → 404, no data leakage | Playwright |
| 1.12 | Back/forward between list and draft pages works correctly | Playwright |
| 1.13 | "New Generation" nav item highlighted on all `/generate/*` routes | Playwright |
| 1.14 | Mock/Sandbox execution mode explicitly displayed via `ExecutionModeTag` | Jest (component) |
| 1.15 | Invalid or contradictory combinations flagged in Draft composition before preview | Jest (component) + Playwright |
| 1.16 | Owner and Member can both create and edit Drafts | Playwright (owner + member fixtures) |

## Slice 2: Submission + Progress

**Scope:** Wire the Submit confirmation, Operation tracking, Run progress page, and RunItem detail.

**Hard prerequisites:**
- Slice 1 merged
- #53 closed (Enriched `GenerationRunItemDetail` with Attempt summary, QC result, cost evidence, bound AssetVersion)

**Files created:**

| File | Type |
|---|---|
| `pages/w/[slug]/generate/runs/[runId].tsx` | New page shell |
| `components/generation/shared/RunProgressSource.ts` | New |
| `components/generation/shared/useRunProgress.ts` | New |
| `components/generation/shared/RunItemStateBadge.tsx` | New |
| `components/generation/preview/SubmitConfirmation.tsx` | New |
| `components/generation/progress/RunProgressView.tsx` | New |
| `components/generation/progress/RunSummary.tsx` | New |
| `components/generation/progress/RunItemGrid.tsx` | New |
| `components/generation/progress/RunItemDetail.tsx` | New |
| `components/generation/progress/OperationTracker.tsx` | New |
| `components/generation/list/RunList.tsx` | New |

**Files modified:**

| File | Change |
|---|---|
| `components/generation/list/GenerationListView.tsx` | Add Run list column alongside Draft list |
| `pages/w/[slug]/generate/drafts/[draftId].tsx` | Wire SubmitConfirmation into preview slot |

**Scope boundary:** This slice does not include recovery actions. The Run page shows progress and RunItem detail in observation mode only. `allowed_actions` for recovery commands are read but not acted upon — their buttons appear disabled with a "recovery not yet available" reason or are hidden.

**Acceptance criteria:**

| # | Criterion | Test type |
|---|---|---|
| 2.1 | Submit requires fresh preview (`preview_id` + `preview_hash`); stale preview rejected by server | Playwright |
| 2.2 | Submit creates Operation; navigates to `/generate/runs/{generation_run_id}` | Playwright |
| 2.3 | Run page finds submission Operation from `Run.commands[]` and polls via `useOperation` | Jest (integration) + Playwright |
| 2.4 | Run page shows RunSummary with status, done/total/failed counters, execution mode, provider, model | Playwright |
| 2.5 | RunItemGrid displays paginated items with correct state badges; page navigation works | Playwright |
| 2.6 | RunItemDetail shows latest Attempt summary (dispatch certainty, outcome), QC result, cost, and bound AssetVersion | Playwright |
| 2.7 | Adaptive polling: 1–2s during `running`, 10–30s during `queued`, stopped when both Run and Operation are terminal | Jest (RunProgressSource) |
| 2.8 | Operation `uncertain` continues polling (not terminal); displayed as "Outcome unconfirmed — waiting for resolution" | Jest (RunProgressSource) + Playwright |
| 2.9 | Transport failure (poll fails) shows "Connection issue — retrying", does not create terminal state or new intent | Jest (RunProgressSource) |
| 2.10 | Operation business failure (`failed`) shows error summary, distinct from transport failure and Operation uncertainty | Jest (component) |
| 2.11 | Browser refresh on `/generate/runs/{runId}` restores Run progress from server; resumes Operation polling from `commands[]` | Playwright |
| 2.12 | Tab-away and tab-back triggers immediate `forceRefresh` (single listener in RunProgressSource) | Jest (useRunProgress) |
| 2.13 | Double-click submit produces single Operation (IntentTracker deduplication) | Jest (integration) |
| 2.14 | Run list on `/generate` shows Runs with latest status | Playwright |
| 2.15 | Mock/Sandbox provenance displayed on Run, RunItems, and provider/model/version on RunItemDetail | Playwright |
| 2.16 | Owner and Member can both submit and observe Runs | Playwright (owner + member fixtures) |

## Slice 3: Recovery

**Scope:** Wire all recovery actions: partial-success handling, cancel, retry-failed, resume-paused, and reconciliation.

**Hard prerequisites:**
- Slice 2 merged
- #51 closed (Recovery preview endpoint returns real data, with `id`, `preview_hash`, `currency`, and `GET`-by-ID support — see Section 6 design gap)

**Files created:**

| File | Type |
|---|---|
| `components/generation/recovery/RecoveryPanel.tsx` | New |
| `components/generation/recovery/RecoveryPreviewDisplay.tsx` | New |
| `components/generation/recovery/ReconciliationGuidance.tsx` | New |
| `components/generation/recovery/CancelConfirmation.tsx` | New |

**Files modified:**

| File | Change |
|---|---|
| `pages/w/[slug]/generate/runs/[runId].tsx` | Wire `recoverySlot` with RecoveryPanel |

**Scope boundary:** This slice completes the #48 scope. It does not extend into Assets (lightbox, review, regeneration, export) — those are tracked by #49.

**Acceptance criteria:**

| # | Criterion | Test type |
|---|---|---|
| 3.1 | Partial success: successful Assets preserved, only failed/paused items shown as recoverable | Playwright |
| 3.2 | Recovery actions appear only when `allowed_actions` declares them with the correct action enum value | Jest (component) + Playwright |
| 3.3 | Cancel in-flight: uncertain items routed to reconciliation, not reported as cancelled | Playwright |
| 3.4 | Cancel confirmation warns about in-flight items before executing | Playwright |
| 3.5 | Retry-failed: recovery preview shows targets + estimated additional cost (Decimal + currency) | Playwright |
| 3.6 | Retry-failed requires fresh recovery preview (`preview_id` + `preview_hash`); stale → rejection | Playwright |
| 3.7 | Resume-paused: recovery preview shows targets + estimated additional cost | Playwright |
| 3.8 | Reconcile: guidance panel explains uncertain items; operator note required | Playwright |
| 3.9 | Recovery command creates Operation; OperationTracker shows progress; Run polling resumes | Playwright |
| 3.10 | Browser refresh during recovery returns to Run observation view (transient recovery preview is lost; user re-requests) | Playwright |
| 3.11 | Expired recovery preview disables confirm and shows "Preview expired — regenerate" | Jest (component) |
| 3.12 | Owner and Member can both perform recovery actions | Playwright (owner + member fixtures) |
| 3.13 | Worker restart during recovery: successful items not redone, failed items resumable | Playwright (with worker kill fixture) |

---

# Section 5: Cross-Cutting Concerns

## Authorization

Both Owner and Member roles can operate the entire New Generation flow. The UI does not gate any generation action by role. Authorization is enforced server-side:

- The BFF's delegated JWT carries `role` and `membership_id` claims.
- The Domain API checks workspace membership and role via `DelegatedIdentityMiddleware`.
- PostgreSQL RLS policies enforce `marketai_context_is_current(workspace_id)`.

The UI's responsibility is limited to:
1. Rendering `allowed_actions` faithfully — if the server says ineligible, the button is disabled.
2. Never constructing cross-workspace requests — the `DomainGateway` always sends to the BFF, which derives workspace from the session.
3. Never embedding workspace ID in client-side URLs beyond the slug.

## Execution-mode provenance

Every view that displays Generation entities must show their execution mode. The contract (§2.2) requires: "页面、运行记录、任务项、产物和导出 Manifest 必须记录运行模式、Provider、模型或 API 版本、模拟场景和关联 ID。"

The `ExecutionModeTag` component wraps the existing `ModeProvenance` component with generation-specific context:

- **Draft**: shows the selected execution mode from `GenerationDraftDetail.execution_mode`.
- **Preview**: shows the resolved mode from `GenerationPreviewDetail.execution_mode`.
- **Run**: shows the frozen mode from `GenerationRunDetail.execution_mode`, plus provider and model.
- **RunItem**: shows the item-level mode from `GenerationRunItemDetail.execution_mode`, plus provider attempt's provider/model/version/scenario when the enriched projections are available.

Mock and Sandbox modes use visually distinct treatments (color, label) that cannot be confused with production/BYOK mode.

## Accessibility

All new components follow the patterns established by the existing workspace components:

- **Focus management**: Route transitions focus the `<h1>` via the existing `WorkspaceLayoutInner` effect.
- **Live regions**: State transitions (RunItem state changes, Operation completion) use `aria-live="polite"` following the `OperationStatus` pattern.
- **Keyboard navigation**: All interactive elements are keyboard-accessible. The RunItemGrid supports arrow-key navigation between items.
- **Screen reader labels**: State badges use `aria-label` with full state names (not just color).
- **Skip links**: The existing skip-to-content link in `WorkspaceLayout` covers all sub-routes.

## Error states

Every applicable view must handle the full state spectrum (v9 contract §2.1):

| State | Display |
|---|---|
| Empty (no drafts/runs) | `FeatureEmptyState` with "New Draft" action |
| Loading | Skeleton/spinner (SWR `isLoading`) |
| Queued | Status badge + "Waiting to start" |
| Running | Animated progress indicator + live counters |
| Partial success | Warning banner + success count + failed/recoverable count |
| Succeeded | Success banner + asset count |
| Failed | Error banner + error summary + retry guidance |
| Retryable | Error banner + "Retry" action (from `allowed_actions`) |
| Cancelled | Info banner + preserved assets count |
| Uncertain (Operation) | Warning banner + "Outcome unconfirmed — waiting for resolution" + continued polling |
| Uncertain (Run items) | Warning banner + reconciliation guidance + reconcile action |
| Permission denied | `ForbiddenState` (existing component) |
| Entitlement limit | `CapabilityNotice` (existing component) |
| Transport error | Inline warning "Connection issue — retrying" (distinct from business errors) |

## Testing strategy

### Unit tests (Jest + Testing Library)

- **Hooks**: `useRunProgress`, `useGenerationNav` — mock `DomainGateway`, assert correct polling intervals, state transitions, and cleanup.
- **Components**: Each feature component tested in isolation with mock data. Assert rendering, ARIA attributes, and user interactions.
- **RunProgressSource**: Test adaptive polling logic, backoff, visibility recovery (single listener), transport error handling, Operation `uncertain` continues polling, pagination-aware item fetching.
- **IntentTracker integration**: Verify that double-submit is deduplicated; retained ambiguous intents past safe-retry window surface `ReconciliationRequired` and persist until explicitly resolved.

### Integration tests (Jest + jsdom)

- **Draft CAS flow**: Create draft → update with `If-Match` → send wrong ETag → 412 → revalidate.
- **Preview stale detection**: Create preview → advance clock past TTL → submit rejected.
- **Operation lifecycle**: Submit → receive `OperationRef` → navigate → Run page finds Operation in `commands[]` → poll → terminal state.

### E2E tests (Playwright)

- **Per-slice acceptance criteria** as listed in Section 4.
- **Cross-slice golden path** (after Slice 3): New Draft (setup dialog) → compose (exact versions) → preview → submit → progress → partial failure → retry → success.
- **Refresh/navigation matrix**: Every route × (refresh, back, forward, deep link).
- **Authorization matrix**: Owner CRUD, Member CRUD, cross-workspace 404.
- **Worker restart**: Kill worker during run, verify recovery.

Playwright fixtures extend the existing `e2e/workspace/fixtures.ts` with generation-specific setup (create draft via API, submit run via API, seed mock provider responses).

---

# Section 6: Hard Prerequisites and Acceptance Matrix

## Backend blockers (GitHub issues)

| Issue | Endpoint | Required by |
|---|---|---|
| #50 | `GET /generation/capabilities` | Slice 1 (Draft composition needs capability data) |
| #51 | `POST /generation/runs/{id}/recovery-previews` | Slice 3 (Recovery needs cost preview) |

### Design gap in #51

The current `GenerationRecoveryPreview` response model has no `id`, no `preview_hash`, and no `currency` field. There is no `GET` endpoint to re-read a recovery preview by ID. This means:

- The retry/resume commands (`RetryFailedItemsInput`, `ResumePausedItemsInput`) require `preview_id` + `preview_hash`, but the recovery preview response cannot provide them.
- The preview cannot be restored after a page refresh.

**Resolution required before Slice 3**: #51 must expand the `GenerationRecoveryPreview` response to include `id` and `preview_hash` (matching the submission preview pattern), plus `currency` for cost display. Whether a `GET` endpoint is also needed depends on whether recovery preview refresh-persistence is required (this spec's current design treats the preview as transient — a refresh returns to observation mode). Update #51's acceptance criteria accordingly.

## Interface enrichment prerequisites

The current response models are intentionally thin (see `response_models.py` header comment). The UI spec requires fields that do not yet exist in the OpenAPI contract. These must be added before the consuming slice can ship.

### #52: Enriched `GenerationPreviewDetail` (required for Slice 1)

Current fields: `id`, `preview_hash`, `recipe_input_hash`, `execution_mode`, `valid_until`, `draft_id`, `draft_revision`, `invalidation_reason`, `allowed_actions`.

**Missing fields needed by the Preview display**:
- `combination_count: int` — total combinations in the resolved preview
- `call_count: int` — number of external API calls (combinations × variants)
- `maximum_cost: { amount: str, currency: str }` — Decimal cost estimate with currency
- `final_prompt: str` — the fully assembled prompt text (auditable)
- `items: list[PreviewItemSummary]` — per-combination: dimension tags, reference assets, exclusion reason if excluded
- `capability_decisions: list[CapabilityDecision]` — provider capability resolution results
- `exclusion_summary: { excluded_count: int, reasons: list[str] }` — why combinations were excluded

### #53: Enriched `GenerationRunItemDetail` (required for Slice 2)

Current fields: `id`, `ordinal`, `state`, `combination_fingerprint`, `variant_index`, `tags`, `allowed_actions`, `combination_identity`, `execution_mode`, `error_summary`, `source_asset_version_id`, `source_run_item_id`, `intent_kind`.

**Missing fields needed by RunItem detail display**:
- `latest_attempt: AttemptSummary | null` — dispatch certainty, outcome, provider, model, version, scenario, cost evidence
- `qc_result: QcResultSummary | null` — latest QC attempt state and check results
- `bound_asset_version_id: str | null` — the produced or reused AssetVersion
- `cost_evidence: CostEvidence | null` — reservation, settlement, or release state with amount and currency

### #54: Conditional-header gateway support (required for Slice 1)

`DomainGateway.sendMutating()` and `runCommand()` must accept and forward `If-Match`. `useDomainCommand.execute()` must accept `ifMatch` in its params. This is a web/ change, not a backend change — the FastAPI endpoints and BFF proxy already support conditional headers.

### Safe draft creation replay (verify during Slice 1)

If the browser crashes between receiving the `create_generation_draft` 201 response and completing the `router.push` navigation, the user may re-click "New Draft". The idempotency key prevents duplicate creation, but the UI should handle the replay gracefully — returning the existing Draft ID and navigating to it. Verify that `IntentTracker` + the 201 replay path covers this.

## Slice dependency chain

```
#50 (capabilities)  ─────────────────────┐
#52 (preview enrichment)  ───────────────┤
#54 (conditional-header gateway) ────────┤
                                         ▼
                                    Slice 1 (Draft + Preview)
                                         │
#53 (RunItem enrichment)  ───────────────┤
                                         ▼
                                    Slice 2 (Submission + Progress)
                                         │
#51 (recovery preview, expanded) ────────┤
                                         ▼
                                    Slice 3 (Recovery)
```

Each slice ships as an independent PR that merges to `main`. No slice may be started until its prerequisites are closed/merged.

#48 becomes `ready-for-agent` only after this spec is approved **and** the prerequisite issues (#50, #51, #52, #53, #54) are closed.

## Full acceptance matrix

This matrix consolidates all per-slice criteria into the eight verification categories required by the v9 contract (§6):

| Category | Slice 1 | Slice 2 | Slice 3 |
|---|---|---|---|
| **User behavior matches v9 handbook** | Draft CRUD with exact versions, Library selection, Seed config, Preview with combination/call/cost display | Submit confirmation, Run/RunItem progress with Attempt/QC/cost/Asset detail | Cancel, retry, resume, reconcile, partial success handling |
| **State from persistent domain truth** | Draft from API, Preview from API, Capabilities from API | Run/RunItem/Operation from API, Operation recovered from Run.commands[] | Recovery preview from API (transient), recovery commands from API |
| **Refresh/restart recovery** | Refresh restores Draft + Preview from server | Refresh restores Run progress; Operation resumed from Run.commands[] | Refresh returns to observation mode; recovery preview re-requested |
| **Cross-area stable links** | Draft references exact Library versions (ProductVersion, PersonVersion, etc.) | Run references frozen recipe snapshot | Recovery references specific RunItems by server ID |
| **Controlled Mock evidence** | ExecutionModeTag on Draft and Preview | ExecutionModeTag on Run, RunItem, and Attempt provider/model/version | ExecutionModeTag on recovery preview |
| **Authorization (Owner + Member)** | Both create/edit Drafts | Both submit and observe | Both perform recovery |
| **Cross-Workspace isolation** | Foreign resource ID → 404 (RLS); membership denial → 403 | Foreign resource ID → 404; membership denial → 403 | Foreign resource ID → 404; membership denial → 403 |
| **Deep link + navigation** | Direct URL, refresh, back/forward | Direct URL, refresh, back/forward | Transient recovery state; refresh returns to observation |

## What this spec does NOT cover

- **Creative Library authoring** — tracked by #47.
- **Asset lightbox, review, regeneration, export** — tracked by #49.
- **Regeneration intents** (`same_recipe`, `new_seed`, `edit_prompt`, `quality_feedback`) — these enter via `createGenerationRegenerationDraft` from an AssetVersion, tracked by #49. This spec's draft creation always uses `new_generation`.
- **Backend Generation domain logic** — delivered in PR #45; stubs in #50 and #51.
- **BFF bridge infrastructure** — delivered in PR #46.
- **Pixel-level design** — the v9 contract explicitly excludes pixel-level replication (§1). Component styling follows existing DaisyUI patterns.
- **SSE real-time updates** — deferred behind the `RunProgressSource` abstraction. Polling is the initial transport.
