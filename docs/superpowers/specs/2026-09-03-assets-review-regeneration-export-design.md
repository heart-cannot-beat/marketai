# Issue #49: Assets, Review, Regeneration & Export — Architectural Design

**Date:** 2026-09-03 (rev 5)
**Issue:** [#49](https://github.com/Raybbish/marketai/issues/49)
**Scope:** Replace the `assets.tsx` empty state with a production browser UI for gallery browsing, human quality review, regeneration, and export workflows.
**Entry point:** `web/pages/w/[slug]/assets.tsx`

---

## 0. Contract Completion Phase

The current server seam cannot support the acceptance criteria as written. Phase 0 is a hard gate: every item below lands with domain, OpenAPI, BFF, and adapter tests before gallery UI work begins. A UI test may use a Controlled Mock provider adapter, but it must exercise the same persistent records, committed blobs, Jobs, Operations, and read projections as production.

### 0.1 Committed Asset Media and the File Seam (P0)

The asset download route is currently a 501 and `AssetVersionRecord.object_key` is not an exact committed reference. Complete the file lifecycle before rendering images:

1. Persist `committed_object_key` and `committed_storage_version_id` on every browsable `AssetVersion`, together with the existing checksum, size, and verified MIME type. Generation succeeds only after the provider output, including Controlled Mock output, has been committed through `BlobStore`; a fabricated key is not a successful asset.
2. Add a workspace-scoped `resolve_asset_blob_ref(version_id)` domain function. It returns a `CommittedBlobRef` only when all committed-reference fields are present. Strengthen `BlobStore.issue_download()` to verify that exact revision before granting it, and add `open_committed(ref)` for Worker-side streaming reads. Missing, uncommitted, changed, or gone objects fail with stable `asset_blob_unavailable`; selectors never silently drop them.
3. Implement `create_generation_asset_version_download_intent` as an idempotent `201 Created` command. It uses `create_or_replay_intent()`, resolves the exact committed ref server-side, persists the download-intent metadata, calls `BlobStore.issue_download()`, and returns `DownloadIntentResponse { intent_id, expires_at, download_grant }`. The browser never supplies an object key.
4. The export download-intent route uses the same deep module and response model. Replays return the same intent record and mint a fresh short-lived grant for the same committed ref.
5. Extend `BrowserBlobTransferClient` with `downloadBlob(grant, { signal }): Promise<Blob>`. The adapter validates the exact storage origin, uses `credentials: 'omit'` and `redirect: 'error'`, rejects non-2xx responses, and exposes only a sanitized transfer error.
6. `useAssetImageUrl` obtains or resumes a Workspace/version-scoped recoverable action identity before requesting the grant, calls the injected `downloadBlob` adapter with an `AbortController`, creates a `blob:` URL, and revokes it on replacement/unmount. A lost response reuses the same action identity. Domain problems expose their stable code, reconciliation remains blocking, and cancellation prevents every later state update.
7. Saving an export uses a small `useGrantedDownload` hook that accepts an already-issued grant, calls `validatedDownloadUrl()`, then `navigateTo()`. The existing `useDownload.requestDownload()` remains the owner-ref → generic platform-intent flow and is not passed a generation grant.

No CSP change is required: storage fetches use `connect-src`; the rendered image uses the existing `blob:` allowance in `img-src`.

### 0.2 Typed Asset Gallery and Detail Projections (P0)

Replace the generic `Page[Any]` asset response with a dedicated `AssetGalleryPage { items: AssetVersionSummary[], next_cursor, facets, allowed_actions }`. Replace the current in-memory/offset slicing with a database keyset cursor over `created_at DESC, id DESC`; the opaque cursor binds the last key to workspace plus the canonical filter hash, and a mismatched cursor fails as `invalid_cursor`. Collection `allowed_actions` includes export eligibility for the current scope.

`list_generation_assets` accepts server-side filters for `product_id`, `run_id`, `provider`, `origin_kind`, `execution_mode`, effective `review_value` (including `unreviewed`), `aspect_ratio`, `qc_state`, stable dimension `tag`, and contextual `conclusion` (`winner | learning | loser | none`). `conclusion` requires an explicit `experiment_context`; otherwise the server returns `experiment_context_required`. The context selects the conclusion projection without turning it into a global field. Facets, including contextual conclusion facets, are computed by the server over the matching Workspace dataset, not inferred from the currently loaded page.

`AssetVersionSummary` contains the fields cards, filters, and review traversal actually use:

```
id, asset_id, product_id, run_id, provider, model, status,
aspect_ratio, stable_dimension_signature, tags,
effective_review_value, latest_qc_state,
contextual_conclusion (only for requested experiment_context),
origin_kind, execution_mode, mock_evidence, byte_size, created_at
```

`AssetVersionDetail` additionally contains:

```
requested/effective seed + unavailable reason
full prompt and immutable recipe/run snapshot identifiers
provider attempt, usage, and cost evidence
reference assets and evidence
QC attempt history + latest_qc_attempt_id
review_head_id, effective_review, append-only reviews[]
contextual experiment conclusions[]
parent + children lineage entries with regeneration intent
safe committed-blob availability/checksum/size/MIME metadata; raw object keys remain server-side
allowed_actions[]
```

An experiment conclusion is returned with its full context; it is never collapsed into a global `winner`/`loser` property.

### 0.3 Idempotent Review and Effective-Review Semantics (P0)

Review creation is a real idempotent command:

1. `CreateReviewInput.review_value` is the enum `approved | needs_changes | rejected | cleared` and accepts `supersedes_review_id`.
2. The route fingerprints method, path, workspace/actor, and body, then calls `create_or_replay_intent()`. The create callback appends one review; the replay callback returns that same review. `AssetReview` includes `supersedes_review_id`.
3. Add one server function, `project_effective_review(version_id)`, and use it for summaries, detail, filtering, export, and `is_ready_for_experiment()`. The review head is the newest row (ordered by `created_at, id`) that is not superseded; this deterministic rule also handles legacy data with multiple roots. A `cleared` head yields `effective_review = null` and therefore removes experiment readiness.
4. The command locks the AssetVersion row, recomputes the head, then appends. If a head exists, another A/C/X/cleared command must name it in `supersedes_review_id`; omission returns `409 effective_review_exists`. The lock prevents concurrent initial roots; the existing unique constraint closes concurrent corrections with `409 review_already_superseded`.
5. The UI always submits A/C/X against `review_head_id` when present. Back/Correct uses the same command path; “undo” appends `cleared` and never deletes history.

This removes the current bug where any historical Approved row keeps an asset ready after a correction.

### 0.4 Real Asynchronous Export (P0)

Export is a persistent workflow, not a UI simulation.

**Selector contract.** `CreateExportInput.selector` is a discriminated union:

```typescript
type ExportSelector =
  | { kind: 'explicit'; version_ids: string[] }
  | { kind: 'winner_only'; scope: ContextualAssetScope }
  | { kind: 'approved_only'; scope: AssetScope }
  | { kind: 'all_successful'; scope: AssetScope };

interface AssetScope {
  product_id?: string;
  run_id?: string;
  provider?: string;
  origin_kind?: string;
  execution_mode?: 'byok' | 'sandbox' | 'mock';
  review_value?: 'approved' | 'needs_changes' | 'rejected' | 'unreviewed';
  aspect_ratio?: string;
  qc_state?: string;
  tags?: string[];
  experiment_context_id?: string;
  conclusion?: 'winner' | 'learning' | 'loser' | 'none';
}

type ContextualAssetScope = Omit<AssetScope, 'experiment_context_id'> & {
  experiment_context_id: string;
};
```

`AssetScope` is canonicalized and validated by the server; tags are deduplicated and sorted before fingerprinting, and `conclusion` requires `scope.experiment_context_id`. The server resolves that ID to the full `ExperimentContextRef` (experiment, market, placement, landing-page version, and Offer version). `winner_only` always requires a `ContextualAssetScope`; the other presets require it whenever their active scope includes a contextual conclusion filter. Winner selection uses confirmed, non-revoked conclusions for the exact `AssetVersion` in that context. The server resolves and freezes exact version IDs; the browser does not implement preset predicates or trust client-supplied context fields.

**Accepted response.** `POST /generation/exports` returns `202 ExportAccepted { package_id, operation_id, state: 'queued' }` plus `Location: /api/v1/operations/{operation_id}`; the BFF mechanically rewrites that header to the same-origin Operation URL. In the same transaction it:

- creates or replays the Operation and package from the idempotency key;
- freezes ordered exact version IDs, selector context, and a provenance snapshot;
- enqueues one durable `generation.export_package` Job bound to the Operation.

`GET /generation/exports/{id}` returns `ExportPackageDetail` including `operation_id`, package state, revision, execution mode, frozen logical count/bytes, error code, committed ZIP availability/hash/size when ready, and timestamps. Raw object keys and storage-version identifiers remain server-side. The package record is server authority for refresh recovery.

**Worker contract.** The Worker handler:

1. claims the Job and advances Operation `queued → running` and package `pending → building`;
2. reloads the frozen selection and opens every exact committed blob revision through `BlobStore.open_committed()`; one unavailable blob fails the whole package with `asset_blob_unavailable`;
3. builds the full manifest from the frozen provenance snapshot;
4. sorts entries and archive paths deterministically, writes fixed ZIP timestamps/permissions, and includes `manifest.json`;
5. stores one physical payload per `content_sha256`; every logical AssetVersion remains a manifest entry pointing to its archive path, so deduplication never erases provenance;
6. commits the ZIP through a new server-owned `BlobStore.commit_generated_artifact(source, spec)` interface implemented by both S3 and local adapters, returning an exact `CommittedBlobRef`;
7. atomically records ZIP key, storage version, SHA-256, byte size, dedup statistics, and `ready`, then marks the Operation and Job succeeded.

The content-addressed target plus deterministic ZIP makes a crash retry safe. A permanent failure records package `failed`, Operation `failed`, and a stable problem code. Controlled Mock exports contain real deterministic mock image bytes and full Mock evidence; placeholder keys/hashes never reach `ready`.

Contract tests kill and restart the Worker before the first blob read, during ZIP assembly, after object commit but before database finalization, and after database finalization but before Job acknowledgement. Lease recovery reuses the same Job, Operation, package, and content-addressed target; it never duplicates a package or ZIP. If storage commit outcome cannot be proven, the Operation remains `uncertain` while recovery probes the exact target revision; ordinary package retry stays disabled until reconciliation produces a definitive result.

### 0.5 Full Export Manifest Contract (P1)

The manifest is a persisted immutable snapshot of export-time truth. It includes package execution mode, selector/context, creation time, logical count, unique-blob count, duplicate count, total logical bytes, archive bytes, and per-entry:

```
asset_version_id, asset_id, combination_display_number,
stable_dimension_signature, all_dimension_tags,
provider, model, provider attempt + usage/cost evidence,
requested/effective seed + unavailable reason,
effective human review + review id,
contextual experiment conclusion + experiment/market/placement/LP/Offer,
QC state and evidence refs, reference-asset refs,
full prompt, recipe snapshot id, generation run + run item,
parent version + regeneration intent,
content SHA-256, opaque committed-blob evidence reference (never an object key), byte size, MIME,
origin kind, execution mode, Mock evidence, archive_path
```

Manifest generation fails closed when required provenance cannot be anchored to the exact version. `GET /generation/exports/{id}/manifest` returns it only for a ready package.

### 0.6 Regeneration Lineage and Variant Counts (P1)

`CreateRegenerationDraftInput.intent` is the enum `same_recipe | new_seed | edit_prompt | quality_feedback`. It also accepts `requested_variant_count` (positive, server-bounded), while `quality_feedback` requires the selected version’s `latest_qc_attempt_id`. Regeneration-draft creation uses `create_or_replay_intent()`; the same idempotency key/body returns the same draft, while a reused key with a different source or body conflicts.

Regeneration stores `source_asset_version_id`, `source_run_item_id`, intent, prompt/QC overrides, and requested variant count in the draft and immutable recipe snapshot. Submission copies them to every created `RunItem`; the Worker writes `AssetVersion.parent_version_id = RunItem.source_asset_version_id`. The detail projection returns parent and children directly; the browser never derives lineage from `producing_run_item_id`.

Review-summary regeneration crosses one deep server Module rather than reproducing grouping rules in the browser:

```typescript
interface ReviewSummaryRegenerationInput {
  action: 'regenerate_needs_changes' | 'supplement_approved';
  candidate_version_ids: string[];
}

interface ReviewSummaryRegenerationResult {
  drafts: Array<{
    stable_dimension_signature: string;
    source_asset_version_id: string;
    generation_draft_id: string;
    requested_variant_count: 2 | 3;
  }>;
}
```

`POST /generation/regeneration-draft-batches` (`create_generation_review_summary_regeneration`) is an idempotent, atomic command. The browser supplies only the exact session candidate version IDs and the selected action. The server scopes and deduplicates candidates, recomputes their effective reviews, groups by stable dimension signature, and chooses the newest exact source by `(created_at DESC, id DESC)`. Needs Changes requests **2 new assets** per signature; Approved supplementation requests **3**. A stale, cross-Workspace, wrong-review, missing-signature, or ineligible candidate fails the whole command with a stable code. Replay returns the same ordered Draft result; no caller reconstructs the grouping or requested counts.

“Skip Loser combinations” belongs to the next generation/regeneration preview, not review traversal. With an experiment context it defaults to enabled; the user may explicitly turn it off before preview. The server excludes stable signatures with a confirmed Loser conclusion in that context and returns `skipped_signature_count`, `skipped_call_count`, and the authoritative cost saving estimate in the preview.

### 0.7 Contextual Experiment Conclusions (P0)

Winner/Learning/Loser is a conclusion about an exact AssetVersion plus experiment, market, placement, landing-page version, and Offer version. Add a read projection over the authoritative experiment decision records with that full context. Legacy `VerdictDecisionRecord` rows may be surfaced only when they can be anchored to an exact AssetVersion and complete context; otherwise they are excluded from Winner presets and Loser skipping.

```typescript
interface ExperimentContextRef {
  context_id: string; // opaque stable reference used in the URL
  experiment_id: string;
  market: string;
  placement: string;
  landing_page_version_id: string;
  offer_version_id: string;
  metric_snapshot_id: string;  // immutable ref to the metric set that produced the verdict
  rule_version_id: string;     // immutable ref to the decision-rule version applied
}

interface ContextualExperimentConclusion {
  decision_id: string;
  asset_version_id: string;
  verdict: 'winner' | 'learning' | 'loser';
  context: ExperimentContextRef;
  evidence_refs: string[];
  confirmed_at: string;
  revoked_at: string | null;
}
```

The gallery may show a contextual badge or apply its Winner/Learning/Loser/none filter only after the user selects an experiment context. The lightbox lists all conclusions with their contexts. Contextual gallery filtering, Export `winner_only`, generation Loser exclusion, and savings calculations run server-side against an explicit context and fail with `experiment_context_required` when it is absent.

### 0.8 Batch Rail Contract Dependency (P1)

`GenerationRunSummary` gains `revision`, `allowed_actions`, and server-computed item aggregates for `pending`, `queued`, `running`, `qc_pending`, `succeeded`, `reused`, `failed`, `paused`, `cancelled`, and `uncertain`. The rail displays those aggregates rather than inferring them from the run status.

Issue #48 owns the Run summary, recovery-preview, retry/resume/reconcile, and recipe-reuse domain implementations. Issue #49 must not implement a second copy. Its Phase 0 gate instead requires the #48 contracts and generated operations to be present: every run command is rendered from `allowed_actions`, sends the current `revision` in `If-Match`, and refreshes the run after completion/conflict; retry/resume uses the immutable recovery preview; reuse calls the server command against the immutable recipe snapshot. If a required #48 operation is unavailable, the #49 production page remains behind the Phase 0 gate; there is no client reconstruction or degraded command fallback.

### 0.9 Refresh and Lost-Response Recovery (P0)

State ownership is explicit:

| State | Authority | Recovery |
|---|---|---|
| Filters, selected asset, overlay, export package | URL | Parse and canonicalize |
| Asset/review/export records | Server | Workspace-scoped GET |
| Command intent identity | `IntentTracker` in `localStorage` | Replay same idempotency key or require reconciliation |
| Review queue snapshot and session counters | `sessionStorage` | Restore by scope hash |
| Loaded gallery pages and transient selection | SWR / React memory | Re-fetch; selection may reset |

Before a review, regeneration-draft, review-summary regeneration, export-create, Asset media download-intent, or Export download-intent request, the UI persists `{ actionInstanceId, operationId, pathParams, body }` in `localStorage` under a Workspace/resource-scoped pending-action key. A definitive server response clears it. After a lost response or refresh, the UI replays the same command/path/body and action identity within the safe retry window; the server returns the same review, Draft result, package, or download-intent record. If `IntentTracker` requires reconciliation, the UI blocks a conflicting new action and presents reconciliation rather than minting a new intent.

For export creation, this pending action recovers the case where the response was lost before `package_id` reached the URL. Once known, `?export=<package_id>` plus `GET /exports/{id}` recovers `operation_id` and state; `sessionStorage` is only a cache.

On review entry, persist `{ scopeHash, orderedVersionIds, nextCursor, currentIndex, decisions, counters }`. Refresh restores that snapshot and fetches the `asset` in the URL directly. If the snapshot is absent, page from the beginning under the same filters until the active asset is found or the cursor is exhausted; never rebuild a later-page queue from page one alone.

### 0.10 Contract Completion Checklist

| # | Change | Completion criterion |
|---|---|---|
| 1 | Persist and validate exact committed asset refs | Missing/uncommitted/changed blob returns `asset_blob_unavailable`; Controlled Mock creates real bytes |
| 2 | Implement both generation download-intent routes | Idempotent metadata + server-issued grant from exact ref |
| 3 | Deepen BlobStore and `BrowserBlobTransferClient` | Exact-revision read/commit plus origin, credentials, redirect, abort, stable domain problem, reconciliation, and revocation tests pass |
| 4 | Add typed gallery page, filters, and facets | No `Any[]`; every advertised chip, including contextual conclusion, is server-backed |
| 5 | Expand asset detail | Lightbox fields, effective review, context, QC, lineage, and Mock scenario/evidence are present |
| 6 | Make review creation idempotent | Lost response/replay appends exactly one row |
| 7 | Add effective-review projection | cleared/superseded Approved cannot remain ready or filter as Approved |
| 8 | Propagate regeneration lineage | submitted child has `parent_version_id` and intent |
| 9 | Make regeneration commands replayable and add the review-summary batch Interface | Lost response returns the same Draft result; server-owned grouping creates Needs Changes=2 and Approved=3 counts per stable signature |
| 10 | Add contextual experiment projection | Winner/Loser is never treated as a global asset property |
| 11 | Implement server-side export selectors | winner/approved/successful presets freeze exact version IDs |
| 12 | Enqueue export Job with 202 response | package and Operation recover after lost response |
| 13 | Build and commit a real deterministic ZIP | manifest, dedup, exact ZIP ref, failure transitions, and Worker kill/restart checkpoints are tested |
| 14 | Expand immutable manifest | Every §3.2 provenance field is populated or fails closed |
| 15 | Consume #48 run recovery/reuse contracts | generated #48 actions use eligibility + revision and enqueue exact targets; #49 contains no duplicate implementation |
| 16 | Add pending-action and queue recovery | refresh tests cover review, Asset media grant, and pre-response export loss |
| 17 | Complete Export HTTP recovery | `202` includes Operation `Location`; package GET recovers authoritative state |
| 18 | Regenerate OpenAPI, operation map, and domain types | generated artifacts are clean and BFF allowlist includes every new operation |

---

## 1. Component Architecture

### 1.1 Layout

Two-zone layout matching the prototype's `.assets-body { grid-template-columns: 320px minmax(0, 1fr) }`:

```
┌─────────────────────────────────────────────────────────────┐
│  AssetsPage                                                 │
│ ┌──────────┬──────────────────────────────────────────────┐ │
│ │ BatchRail│  GalleryZone                                 │ │
│ │ 320px    │  minmax(0, 1fr)                              │ │
│ │          │ ┌──────────────────────────────────────────┐ │ │
│ │ Run list │ │ FilterBar (chips: provider, origin, mode,│ │ │
│ │ grouped  │ │ review, aspect, QC, tag, conclusion)     │ │ │
│ │ by       │ ├──────────────────────────────────────────┤ │ │
│ │ product  │ │ AssetGrid (responsive masonry)           │ │ │
│ │          │ │ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐        │ │ │
│ │          │ │ │thumb│ │thumb│ │thumb│ │thumb│ ...     │ │ │
│ │          │ │ └─────┘ └─────┘ └─────┘ └─────┘        │ │ │
│ │          │ │  ...infinite scroll...                   │ │ │
│ │          │ ├──────────────────────────────────────────┤ │ │
│ │          │ │ SelectionBar (count, clear, actions)     │ │ │
│ └──────────┴─┴──────────────────────────────────────────┘ │ │
└─────────────────────────────────────────────────────────────┘
```

On screens < 1024px, the BatchRail collapses into a top horizontal ribbon (CSS-only via `@media`, no `useMediaQuery`). The gallery fills the full width.

### 1.2 Component Tree

```
AssetsPage (page, owns URL state)
├── AssetsProvider (context: filters, selection, gallery data)
│   ├── BatchRail
│   │   ├── RunGroupList (runs grouped by product, shows run states)
│   │   │   └── RunGroupItem (status: queued/running/succeeded/failed/retry/resume)
│   │   └── BatchRailActions (reuse run, retry failed, resume paused)
│   ├── GalleryZone
│   │   ├── FilterBar (chip groups, computed from query params)
│   │   ├── AssetGrid
│   │   │   └── AssetCard[] (thumbnail via blob URL, provenance badge, review indicator)
│   │   ├── SelectionBar (selection count, bulk actions)
│   │   └── InfiniteScrollSentinel
│   ├── LightboxModal (portal, opened by clicking a card)
│   │   ├── LightboxImage (adapter download → revocable blob URL)
│   │   ├── AssetSidebar
│   │   │   ├── ComboTagList
│   │   │   ├── ProvenanceDetail (ModeProvenance + seed/model)
│   │   │   ├── ReviewPanel (current reviews, review+correct buttons)
│   │   │   ├── RegenerationPanel (same recipe, new seed, edit prompt, quality feedback)
│   │   │   └── VersionHistory (lineage chain)
│   │   └── LightboxNav (prev/next arrows, keyboard ←→)
│   ├── ReviewMode (fullscreen overlay, keyboard-driven)
│   │   ├── ReviewCanvas (centered asset)
│   │   ├── ReviewProgress (progress bar + reviewed/skipped counts)
│   │   ├── ReviewSidePanel (metadata, same as AssetSidebar)
│   │   └── ReviewSummary (shown on completion)
│   │       ├── ReviewSummaryStats
│   │       ├── RegenerateNeedsChangesAction (×N)
│   │       ├── SupplementApprovedAction (×N)
│   │       └── GoToExperimentsAction
│   └── ExportFlow (modal wizard)
│       ├── ExportConfigStep (version selection, preset: winner-only/approved-only/all-successful)
│       ├── ExportProgressStep (OperationStatus + polling)
│       └── ExportCompleteStep (manifest summary, download button)
```

### 1.3 Shared Components (reused from existing)

- `ModeProvenance` — provenance badge (Production/Read-only/Sandbox/Controlled Mock)
- `OperationStatus` — async Operation lifecycle display
- `FeatureEmptyState` — empty gallery state
- `WorkspaceLayout` — page layout shell (existing `getLayout` pattern)

---

## 2. URL State Model

The URL owns shareable navigation state: gallery scope, selected asset, overlay mode, experiment context, and export package. Transient multi-selection, command intents, review-queue snapshots, and session counters use the stores assigned in §0.9; they are not misrepresented as deep-link state.

### 2.1 URL Schema

```
/w/[slug]/assets?run=<id>&product=<id>&provider=<p>&origin=<o>&mode=<m>&review=<v>&qc=<s>&aspect=<r>&tag=<t>&experiment_context=<id>&conclusion=<v>&asset=<id>&view=lightbox|review&export=<id>
```

| Param | Type | Default | Purpose |
|-------|------|---------|---------|
| `run` | string | (all) | Filter by run_id |
| `product` | string | (all) | Filter by product_id |
| `provider` | string | (all) | Filter by provider |
| `origin` | string | (all) | Filter by origin_kind |
| `mode` | string | (all) | Filter by execution_mode |
| `review` | string | (all) | Filter by effective review (approved/needs_changes/rejected/unreviewed) |
| `qc` | string | (all) | Filter by latest QC state |
| `aspect` | string | (all) | Filter by aspect ratio |
| `tag` | string or repeated string | (all) | Filter by stable dimension tag |
| `experiment_context` | string | — | Opaque server-issued reference to one full experiment/market/placement/LP/Offer context |
| `conclusion` | `winner` \| `learning` \| `loser` \| `none` | (all) | Filter the selected context's authoritative conclusion projection; invalid without `experiment_context` |
| `asset` | string | — | Selected asset version_id (lightbox or review target) |
| `view` | `lightbox` \| `review` | — | Active overlay mode |
| `export` | string | — | Active export_package_id (export flow open) |

### 2.2 URL Mutation Rules

1. **User navigation** (clicking a filter chip, a card, entering review mode): `router.push` — creates history entry.
2. **Canonicalization** (removing an unknown param, normalizing a defaulted value): `router.replace` — no history entry.
3. **Defaults are implicit**: no `review=all` in URL; absence means "all".
4. **Preserve unrelated params**: closing an overlay does not clear filters. A filter change clears `asset`/`view` when the selected version is outside the new scope.
5. **Invalid asset → graceful degradation**: if `asset=<id>` returns 404, clear `asset` and `view` via `router.replace`.
6. **Cross-run navigation**: changing `run` clears `asset` (the asset may not belong to the new run).
7. **Canonical multi-values**: repeated `tag` values are deduplicated and sorted before `router.replace`, making the scope hash stable.
8. **Context safety**: clearing `experiment_context` also clears `conclusion`, removes contextual Winner/Learning/Loser badges, and disables context-dependent export/skip presets. A deep link with `conclusion` but no context canonicalizes by removing `conclusion`; the server independently rejects that filter as `experiment_context_required`.

### 2.3 URL Parsing Hook

```typescript
function useAssetsUrlState() {
  // Parse: read from router.query, memoize
  // Push: (patch) => router.push with merged params, removing undefined keys
  // Replace: (patch) => router.replace with merged params
  // Returns: { filters, experimentContextId, selectedAssetId, viewMode, exportId, pushState, replaceState }
}
```

---

## 3. Data Fetching Architecture

### 3.1 Gallery Data — Cursor-Based Infinite Scroll

Add a deep `useDomainInfiniteQuery` module beside `useDomainQuery`. Its implementation owns `useSWRInfinite`, domain key construction, workspace scoping, typed query execution, and page errors; feature hooks never create a dynamic number of React hooks.

```typescript
function useAssetGallery(filters: AssetFilters) {
  const query = useDomainInfiniteQuery(
    'list_generation_assets',
    ({ previousPage }) =>
      previousPage && !previousPage.next_cursor
        ? null
        : { ...filters, cursor: previousPage?.next_cursor, limit: '40' }
  );

  return {
    assets: query.pages.flatMap(page => page.items),
    facets: query.pages[0]?.facets,
    loadMore: query.loadMore,
    hasMore: Boolean(query.pages.at(-1)?.next_cursor),
    isLoadingMore: query.isLoadingMore,
    refresh: query.refresh,
  };
}
```

The filter object is part of the first-page key, so a scope change resets pagination. Facet chips come from `AssetGalleryPage.facets`; items use the typed `AssetVersionSummary` from §0.2. Full detail loads on demand.

### 3.2 Asset Detail — On-Demand Fetch

```typescript
const { data: assetDetail } = useDomainQuery(
  'get_generation_asset_version',
  { version_id: selectedAssetId },
  { enabled: !!selectedAssetId }
);
```

Fetched when a card is clicked or review mode advances. The detail contract is the full projection in §0.2. The direct-by-id request is independent of loaded gallery pages, which is required for later-page refresh recovery.

### 3.3 Cache Invalidation Strategy

Export `domainQueryKey()` and `isDomainQueryKey()` from the domain-query module and use them in a workspace-scoped invalidation hook. Feature code does not duplicate serialized key layout:

```typescript
function useAssetCacheInvalidation() {
  const { current } = useWorkspace();
  const slug = current.slug;

  const invalidateGallery = useCallback(
    () => mutate(
      (key: unknown) =>
        isDomainQueryKey(key, {
          workspaceSlug: slug,
          operationId: 'list_generation_assets',
        }),
      undefined,
      { revalidate: true }
    ),
    [slug]
  );

  const invalidateAssetDetail = useCallback(
    (versionId: string) => mutate(
      domainQueryKey(
        slug,
        'get_generation_asset_version',
        { version_id: versionId }
      ),
      undefined,
      { revalidate: true }
    ),
    [slug]
  );

  const invalidateAll = useCallback(
    (versionId?: string) =>
      Promise.all([
        invalidateGallery(),
        versionId ? invalidateAssetDetail(versionId) : Promise.resolve(),
      ]),
    [invalidateAssetDetail, invalidateGallery]
  );

  return {
    invalidateGallery,
    invalidateAssetDetail,
    invalidateAll,
  };
}
```

The predicate requires exact workspace and operation IDs and therefore covers every cursor page without touching another Workspace. Detail invalidation uses the shared exact-key builder. Trigger it after a definitive review or regeneration outcome; export creation does not mutate asset reads.

---

## 4. CommandOutcome — Direct Return from execute()

Modify `useDomainCommand.execute()` to return the outcome directly alongside setting state. The response is already in scope before `setState` calls — return it as a discriminated union:

```typescript
type CommandOutcome<K extends CommandOperationId> =
  | { kind: 'success'; data: SuccessDataFor<K> }
  | { kind: 'problem'; problem: ProblemDetails }
  | { kind: 'reconciliation'; reconciliation: ReconciliationRequired };

// In useDomainCommand, change execute signature:
const execute = useCallback(async (params: ExecuteParams<K>): Promise<CommandOutcome<K>> => {
  lastParamsRef.current = params;
  setResult(undefined);
  setProblem(undefined);
  setReconciliation(undefined);

  const response = await callCommandOperation(
    gateway, operationId, current.slug,
    params.actionInstanceId,
    (params as BodyParam<K>).body as DomainOperationMap[K]['requestBody'],
    params.pathParams
  );

  if (response.kind === 'reconciliation_required') {
    setReconciliation(response as ReconciliationRequired);
    return { kind: 'reconciliation', reconciliation: response as ReconciliationRequired };
  }
  if (response.kind === 'problem') {
    const p = (response as { kind: 'problem'; problem: ProblemDetails }).problem;
    setProblem(p);
    return { kind: 'problem', problem: p };
  }
  const data = (response as Exclude<DomainResultFor<K>, { kind: 'problem' }>).data;
  setResult(data);
  return { kind: 'success', data };
}, [gateway, operationId, current.slug]);
```

Backward compatible: existing consumers still read from `result`/`problem`/`reconciliation` state. New consumers can `const outcome = await execute(params)` for imperative flows. The return type change from `Promise<void>` to `Promise<CommandOutcome<K>>` is non-breaking — callers that ignored the return value continue to work.

---

## 5. Keyboard Review Mode

### 5.1 Review Flow

1. User enters review mode via "Review" button (gallery toolbar or batch rail context).
2. Persist the queue snapshot, then update the URL to `?view=review&asset=<first-queue-version-id>` via `router.push`.
3. Fullscreen overlay renders with the current asset centered.
4. Keyboard actions dispatch review commands or navigate:

| Key | Action | Domain Operation | Outcome |
|-----|--------|-----------------|---------|
| `A` | Approve | `create_generation_asset_review` with `review_value: 'approved'` | Advance to next |
| `C` | Needs Changes | `create_generation_asset_review` with `review_value: 'needs_changes'` | Advance to next |
| `X` | Reject | `create_generation_asset_review` with `review_value: 'rejected'` | Advance to next |
| `Space` | Skip | (no domain call) | Advance to next, no record created |
| `←` | Back | (no domain call) | Return to previous asset |
| `Escape` | Exit | — | Close review mode, show summary |

### 5.2 Permission Safety

Before dispatching a review, check the current asset's `allowed_actions`. The server uses action codes `"review"`, `"regenerate"`, and `"download"` (NOT `create_review` etc.):

```typescript
function canReview(asset: AssetVersionDetail): boolean {
  return asset.allowed_actions?.some(
    a => a.action === 'review' && a.allowed
  ) ?? false;
}
```

If `canReview` is false, the keyboard shortcut is visually disabled (grayed keys in the UI legend) and pressing A/C/X shows a toast with the `reason_code` from the disallowed action. Skip and Back work while idle. While a review command is pending or awaiting reconciliation, all queue movement is locked so Space/Back cannot move the target under the request. The handler ignores editable elements, IME composition, and `event.repeat`, and prevents Space scrolling only inside the active review overlay.

### 5.3 Double-Submit Prevention

A synchronous ref mutex prevents same-render races; a recoverable pending-action record prevents refresh/lost-response duplication:

```typescript
function useReviewMode() {
  const pendingRef = useRef(false);

  async function submitReview(asset: AssetVersionDetail, reviewValue: ReviewValue) {
    if (pendingRef.current) return; // synchronous guard — no React state race
    pendingRef.current = true;

    try {
      const body = {
        review_value: reviewValue,
        reason: '',
        supersedes_review_id: asset.review_head_id,
      };
      // beginOrResume persists body + actionInstanceId before network I/O.
      // An unresolved record must be replayed/reconciled, not replaced.
      const action = recoverableActions.beginOrResume({
        key: `review:${workspaceSlug}:${asset.id}`,
        operationId: 'create_generation_asset_review',
        body,
      });

      const outcome = await execute({
        pathParams: { version_id: asset.id },
        body,
        actionInstanceId: action.actionInstanceId,
      });
      if (outcome.kind === 'success') {
        recoverableActions.complete(action.key);
        advanceToNext();
      }
      if (outcome.kind === 'problem') {
        recoverableActions.complete(action.key); // definitive server answer
      }
      // reconciliation retains the record and blocks a conflicting decision
    } finally {
      pendingRef.current = false;
    }
  }
}
```

`pendingRef` is an immediate UI mutex, not the durability mechanism. `IntentTracker` and the pending-action record keep the same logical action/body across reloads. The server idempotency contract in §0.3 is what guarantees exactly one append.

### 5.4 Review Correction (Undo)

Reviews are append-only. "Undo" is an explicit correction review, not a delete:

1. The Back key (`←`) navigates to a previously reviewed asset.
2. The sidebar shows `effective_review`, `review_head_id`, and a "Correct" button.
3. Clicking "Correct" prompts for a new `review_value` and sends `create_generation_asset_review` with `supersedes_review_id = review_head_id`.
4. A `review_value: 'cleared'` submission (with `supersedes_review_id`) clears the effective review entirely.
5. A/C/X on an already-reviewed version follows the same correction rule; it never starts a parallel review head.
6. The server validates the chain and recomputes the effective projection. The `reviews[]` array remains complete, while readiness, filters, and export use only `effective_review`.

### 5.5 Review Queue

The review queue is the current gallery result set (with active filters). Assets are traversed in gallery order. State tracked in `ReviewModeProvider`:

```typescript
interface ReviewModeState {
  scopeHash: string;
  queue: string[];         // immutable version-id snapshot in gallery order
  nextCursor: string | null;
  currentIndex: number;
  decisions: Map<string, ReviewDecision>;
  // ReviewDecision: { value: string, pending: boolean, reviewId?: string, outcome?: CommandOutcome }
}
```

The queue is initialized from the gallery's flattened asset list when review mode opens and is persisted to `sessionStorage` before the overlay opens. New assets are not added mid-review. The URL's `asset` points at the current exact version; §0.9 defines restoration when the snapshot is missing.

### 5.6 Review Summary

When `currentIndex >= queue.length` or the user presses Escape, the ReviewSummary overlay appears:

```
Review Complete
─────────────────────────────
✓ Approved:        12
⚠ Needs Changes:    3  (2 new assets per stable combination)
✕ Rejected:         1
  Skipped:           4
─────────────────────────────
[Regenerate Needs Changes (3 combos → 6)]  [Supplement Approved (12 combos → 36)]  [Go to Experiments]  [Back to Gallery]
```

**Action buttons:**

- **Regenerate Needs Changes**: Sends the session's exact Needs Changes candidate version IDs to `create_generation_review_summary_regeneration`; the server groups stable signatures, chooses sources, and creates replayable Drafts with `requested_variant_count: 2`.
- **Supplement Approved**: Sends the session's exact Approved candidate version IDs through the same Interface; the server creates same-recipe Drafts with `requested_variant_count: 3` per stable signature.
- **Go to Experiments**: Navigates to the experiments page (cross-page link, out of #49 scope — renders as link only).
- **Back to Gallery**: Closes review mode, returns to gallery with filters preserved.

The resulting generation preview defaults **Skip contextual Loser signatures** on when `experiment_context` is present; the server returns skipped signatures/calls and savings. Review traversal itself never auto-skips an asset because Winner/Loser is context-specific.

### 5.7 Batch Rail Run States

The BatchRail consumes the server summary and its item-state aggregates:

| State | Display | Actions |
|-------|---------|---------|
| `queued` | "Queued" + spinner | — |
| `running` | "Running" + pending/queued/running/QC/paused aggregates | cancel/resume or other actions returned by server |
| `succeeded` | "Complete" + succeeded/reused counts | review/export; reuse when eligible |
| `partially_succeeded` | "Partial" + succeeded/reused/failed/cancelled counts | retry/reuse when eligible |
| `failed` | "Failed" + failed/cancelled counts and stable error | retry/reuse when eligible |
| `cancelled` | "Cancelled" + cancelled count | reuse when eligible |
| `uncertain` | "Needs reconciliation" + exact uncertain count and evidence summary | reconcile only when eligible; never ordinary retry |

Buttons are derived only from `allowed_actions`. Mutating commands send `If-Match: <revision>`; retry/resume first create and confirm a recovery preview, and reuse calls the #48 server command from §0.8. A disallowed action displays its `reason_code`; lifecycle status alone never grants permission. Because `uncertain` has precedence in the server Run projection, it is never folded into `partially_succeeded`, and paused items remain visible under `running` until resumed or cancelled.

---

## 6. Lightbox

### 6.1 Trigger

Clicking an `AssetCard` in the gallery opens the lightbox: `router.push({ query: { ...current, asset: versionId, view: 'lightbox' } })`.

### 6.2 Content

```
┌──────────────────────────────────────────────────────────────┐
│  ← prev                    [✕ close]                 next → │
│ ┌──────────────────────────────┬───────────────────────────┐ │
│ │                              │ AssetSidebar              │ │
│ │   Full-size image            │ ┌───────────────────────┐ │ │
│ │   (adapter → blob URL)       │ │ Combo tags            │ │ │
│ │                              │ ├───────────────────────┤ │ │
│ │                              │ │ Provenance            │ │ │
│ │                              │ │ Provider / Model      │ │ │
│ │                              │ │ Seed (req / effective) │ │ │
│ │                              │ │ execution_mode badge  │ │ │
│ │                              │ ├───────────────────────┤ │ │
│ │                              │ │ Quality Review        │ │ │
│ │                              │ │ [Approve] [NeedsCh]   │ │ │
│ │                              │ │ [Reject] [Correct]    │ │ │
│ │                              │ ├───────────────────────┤ │ │
│ │                              │ │ Regeneration          │ │ │
│ │                              │ │ [Same Recipe]         │ │ │
│ │                              │ │ [New Seed]            │ │ │
│ │                              │ │ [Edit Prompt]         │ │ │
│ │                              │ │ [Quality Feedback]*   │ │ │
│ │                              │ ├───────────────────────┤ │ │
│ │                              │ │ Version History       │ │ │
│ │                              │ │ (lineage chain)       │ │ │
│ │                              │ ├───────────────────────┤ │ │
│ │                              │ │ Full Prompt           │ │ │
│ │                              │ │ (collapsible)         │ │ │
│ └──────────────────────────────┴───────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
* Quality Feedback only shown when latest_qc_attempt_id is non-null
```

### 6.3 Image Loading (adapter + blob URL)

CSP `img-src` is `'self' data: blob:` — storage origins are only in `connect-src`. Direct `<img src={signedUrl}>` pointing at storage origins is blocked. Solution: fetch the image binary via `connect-src`, then create a revocable blob URL for `<img>`.

```typescript
function useAssetImageUrl(versionId: string | null) {
  const { current } = useWorkspace();
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [problem, setProblem] = useState<ProblemDetails | null>(null);
  const [reconciliation, setReconciliation] =
    useState<ReconciliationRequired | null>(null);
  const [transferError, setTransferError] =
    useState<DownloadTransferError | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!versionId) {
      setBlobUrl(null);
      setProblem(null);
      setReconciliation(null);
      setTransferError(null);
      return;
    }
    const controller = new AbortController();
    let ownedObjectUrl: string | null = null;
    setBlobUrl(null);
    setProblem(null);
    setReconciliation(null);
    setTransferError(null);
    setIsLoading(true);

    (async () => {
      try {
        const action = recoverableActions.beginOrResume({
          key: `asset-media:${current.slug}:${versionId}`,
          operationId: 'create_generation_asset_version_download_intent',
          pathParams: { version_id: versionId },
        });
        const outcome = await execute({
          pathParams: { version_id: versionId },
          actionInstanceId: action.actionInstanceId,
        });
        if (controller.signal.aborted) return;
        if (outcome.kind === 'reconciliation') {
          setReconciliation(outcome.reconciliation);
          return; // retain the action identity and keep retry blocked
        }
        if (outcome.kind === 'problem') {
          recoverableActions.complete(action.key); // definitive response
          setProblem(outcome.problem);              // preserve stable code
          return;
        }
        recoverableActions.complete(action.key);

        const blob = await blobClient.downloadBlob(
          outcome.data.download_grant,
          { signal: controller.signal }
        );
        if (controller.signal.aborted) return;

        ownedObjectUrl = URL.createObjectURL(blob);
        setBlobUrl(ownedObjectUrl);
      } catch (cause) {
        // A transport-ambiguous command retains its recoverable action.
        // A Blob transfer failure is sanitized by the Adapter.
        if (!controller.signal.aborted) {
          setTransferError(toSafeTransferError(cause));
        }
      } finally {
        if (!controller.signal.aborted) setIsLoading(false);
      }
    })();

    return () => {
      controller.abort();
      if (ownedObjectUrl) URL.revokeObjectURL(ownedObjectUrl);
    };
  }, [attempt, blobClient, current.slug, execute, recoverableActions, versionId]);

  return {
    blobUrl,
    isLoading,
    problem,
    reconciliation,
    transferError,
    retry: () => setAttempt(value => value + 1),
  };
}
```

The feature hook never calls `fetch` directly. Origin validation, `credentials: 'omit'`, `redirect: 'error'`, response checks, and abort behavior live behind the tested `BrowserBlobTransferClient` seam. The blob URL is revoked on unmount or version change. `AssetCard` and `LightboxImage` render localized recovery for the stable domain `problem.code`, transfer failure, and reconciliation states; none becomes an unexplained blank image.

### 6.4 Keyboard

- `←` / `→`: Previous/next asset in gallery order
- `Escape`: Close lightbox
- `A` / `C` / `X`: Quick review (same as review mode, permission-gated)

---

## 7. Regeneration

### 7.1 Four Regeneration Paths

All four call `create_generation_regeneration_draft` with the typed input from §0.6. A single-asset action defaults `requested_variant_count` to 1:

| Path | `intent` (enum) | `prompt_overrides` | `qc_attempt_id` | `requested_variant_count` |
|------|-----------------|-------------------|-----------------|-----------------|
| Same Recipe | `'same_recipe'` | `null` | `null` | 1 by default |
| New Seed | `'new_seed'` | `null` | `null` | 1 by default |
| Edit Prompt | `'edit_prompt'` | `{ text: "..." }` | `null` | 1 by default |
| Quality Feedback | `'quality_feedback'` | `null` or overrides | exact latest attempt ID | 1 by default |

The Same Recipe confirmation states that Prompt, Seed, model version, and parameters are preserved, but byte-identical output is guaranteed only when the selected Provider explicitly advertises determinism.

### 7.2 Flow

1. User clicks regeneration button in lightbox or review summary.
2. For "Edit Prompt": a modal with the original prompt pre-filled opens; user edits and confirms.
3. For "Quality Feedback": `qc_attempt_id` is taken from `assetDetail.latest_qc_attempt_id` — this field must be non-null for the button to appear. The regeneration domain Module validates that the exact QC Attempt belongs to the same Workspace and source AssetVersion and is still the selected evidence. Each failure has its own stable error code:
   - `qc_attempt_not_found` — the supplied `qc_attempt_id` does not exist.
   - `qc_attempt_wrong_workspace` — the QC Attempt belongs to a different Workspace.
   - `qc_attempt_wrong_version` — the QC Attempt belongs to a different AssetVersion than the regeneration source.
   - `qc_attempt_expired` — the QC Attempt is no longer the latest selected evidence for this version.
4. `create_generation_regeneration_draft` command fires with the source `version_id` and appropriate input.
5. On success: the response is a `GenerationDraftDetail`. The user is navigated to the generation page to preview/submit the draft, OR a toast confirms draft creation with a link.
6. Draft → recipe snapshot → RunItems preserve source and intent. Every successful output sets `parent_version_id` to the exact source version and starts with no inherited human review or experiment conclusion.

### 7.3 Review Summary Batch Regeneration

From the Review Summary:

1. Collect exact candidate version IDs from the effective session decisions; do not group them or choose a source in the browser.
2. “Regenerate Needs Changes” submits `{ action: 'regenerate_needs_changes', candidate_version_ids }` once.
3. “Supplement Approved” submits `{ action: 'supplement_approved', candidate_version_ids }` once.
4. The server atomically revalidates effective review and eligibility, groups by stable signature, chooses the newest source by `(created_at DESC, id DESC)`, and returns the ordered Draft result from §0.6. Replay returns that same result; no partial client fan-out exists.
5. The generation preview, not the review UI, defaults contextual Loser exclusion on and returns skipped counts plus savings.
6. A separate single-version Quality Feedback action is available only when `latest_qc_attempt_id` is present; a human review is never substituted for QC evidence.

### 7.4 Lineage Display

In the lightbox VersionHistory panel:

```
v3 (current) ← regenerated from v2 (new seed)
v2           ← regenerated from v1 (edit prompt)
v1           ← original generation
```

Each entry shows version ID, regeneration intent, stable signature, origin, and effective review. The server returns explicit parent and child lineage entries assembled from `parent_version_id` plus propagated RunItem intent; the browser does not infer the chain from `producing_run_item_id`.

---

## 8. Export

### 8.1 Export Flow (Modal Wizard)

**Step 1: Configure**
- Select a server-backed selector:
  - **"Winner-only"** — exact versions with a confirmed Winner conclusion in the selected `experiment_context`
  - **"Approved-only"** — exact versions whose effective review is Approved
  - **"All successful"** — every exact version with a successful/reused producing RunItem; a missing committed blob fails the package instead of shrinking the preset
  - **"Selected"** — explicit version IDs from gallery multi-select
- The request sends the discriminated selector from §0.4. Presets remain predicates until the server resolves them; the browser does not fetch every page and filter locally.
- Explicit selection can show an immediate count/size. Preset count is shown as “resolved by server” until acceptance, then uses the frozen package totals.
- **Mixed execution mode guard:** explicit loaded selections are checked early for feedback, but the server remains authoritative for every selector. `mixed_execution_modes` includes mode counts so the user can narrow the scope; there is no bypass.
- Before network I/O, persist the recoverable export-create action. Submit calls `create_generation_export_package` and expects `202 ExportAccepted`.

**Step 2: Progress**
- The create call returns `{ package_id, operation_id, state: 'queued' }` and the same Operation in its BFF-rewritten `Location` header.
- Use `useOperation` to poll via `operation_id` and render `OperationStatus`.
- Update the URL to `?export=<package_id>`.
- Revalidate `GET /generation/exports/{id}`; the package owns export-specific state and error details.

**Step 3: Complete**
- Only when Operation is `succeeded` **and** package is `ready`:
  - Fetch manifest via `get_generation_export_manifest`.
  - Display logical/unique counts, sizes, content-hash dedup stats, selector context, and full provenance.
  - "Download" calls `create_generation_export_download_intent`; `useGrantedDownload` validates and navigates to its returned grant.
  - Binary goes directly from storage to the browser — never through Next.js or FastAPI.

### 8.2 Export Manifest

The manifest view renders the immutable contract from §0.5:

```
Export Package: exp_abc123
Execution mode: byok
─────────────────────────────
12 logical assets, 9 unique blobs, 3 duplicates
Logical bytes: 45.2 MB; ZIP bytes: 33.8 MB

Per-entry fields:
  stable signature + every dimension tag; Provider/model/Seed;
  effective review; contextual experiment evidence; QC evidence;
  references; full Prompt/recipe/run/lineage; exact file hash/ref;
  origin/execution mode/Mock evidence; archive path
```

The compact table exposes key fields and expands one logical entry to the complete provenance record. Duplicate logical entries remain visible and point to the same `archive_path`.

### 8.3 Export Download Intent

The route returns `{ intent_id, expires_at, download_grant }` only after the package is ready and its exact committed ZIP revision is available. `useGrantedDownload(grant)` calls the injected adapter’s `validatedDownloadUrl()` and then `navigateTo()`. It never stores or returns the signed URL. The generic `useDownload.requestDownload()` is not reused because it creates a different platform intent from an owner/blob reference.

### 8.4 Recovery

If the user refreshes while `?export=<id>` is in the URL:
1. Parse `export` on mount.
2. Fetch `GET /generation/exports/{id}` and read its authoritative `operation_id`.
3. For `pending`/`building`, resume Operation and package polling.
4. For `ready`, fetch the manifest and show the complete step.
5. For `failed`, show the stable failure code and offer a new explicit retry.
6. For an `uncertain` Operation, keep the package locked, show reconciliation status, and poll the server's exact-target recovery; do not create a replacement package or perform an ordinary retry.
7. A `sessionStorage` package cache may speed the initial paint but never determines state.

If refresh/loss happened before `package_id` reached the URL, the pending export action from §0.9 replays the same body and `actionInstanceId`; the 202 response returns the original package and then installs the URL.

---

## 9. Selection Model

### 9.1 Multi-Select

Gallery cards support multi-select for bulk operations (bulk review, bulk export):

```typescript
interface SelectionState {
  selectedIds: Set<string>;
  lastClickedId: string | null;
}
```

- Click: toggle single selection
- Shift+Click: range select (from `lastClickedId` to clicked card)
- Ctrl/Cmd+Click: toggle without clearing others
- "Select All" in SelectionBar: selects all loaded assets (not all server-side)

### 9.2 SelectionBar

Appears when `selectedIds.size > 0`:

```
┌──────────────────────────────────────────────────────────┐
│ 5 selected   [Clear]   [Review Selected]   [Export]      │
└──────────────────────────────────────────────────────────┘
```

"Review Selected" enters review mode with the queue limited to selected version_ids.
"Export" opens the export flow pre-populated with selected version_ids.

---

## 10. Permissions & Security

### 10.1 Server-Authoritative Actions

The client does NOT derive permissions from roles. All action visibility is controlled by the intersection of:

1. **UI-declared capabilities** — the set of actions the component knows how to render.
2. **Server `allowed_actions`** — the `ActionEligibility[]` array on `AssetVersionDetail`.

The server uses these exact action codes (`run_queries.py:383`):
- `"review"` — can submit a quality review
- `"regenerate"` — can create a regeneration draft
- `"download"` — can request a download intent (gated by `has_blob`)

```typescript
const ASSET_ACTION_CODES = ['review', 'regenerate', 'download'] as const;
type AssetActionCode = typeof ASSET_ACTION_CODES[number];

function useAssetActions(asset: AssetVersionDetail | undefined) {
  if (!asset?.allowed_actions) return {};
  return Object.fromEntries(
    ASSET_ACTION_CODES.map(code => {
      const eligibility = asset.allowed_actions!.find(a => a.action === code);
      return [code, {
        allowed: eligibility?.allowed ?? false,
        reasonCode: eligibility?.reason_code ?? null,
      }];
    })
  );
}
```

**Export eligibility** is a collection-level concern. `AssetGalleryPage.allowed_actions` supplies the server-authoritative `export` action for the active scope; the toolbar and SelectionBar intersect it with the UI capability. The create command revalidates workspace membership, non-empty resolution, exact committed refs, and homogeneous execution mode. A collection action does not get inferred from user role or individual asset status.

Buttons are hidden (not just disabled) when the action is not in `allowed_actions` at all. Buttons are disabled with tooltip when `allowed: false` with a `reason_code`.

`download` eligibility is true only when the record contains a complete exact committed ref, not merely a non-empty object-key string. Intent creation and export then revalidate the live storage revision, closing the race between a read projection and use. `asset_blob_unavailable` is the consistent failure code.

### 10.2 Cross-Workspace Isolation

- All queries and commands are scoped to `useWorkspace().current.slug`.
- The BFF re-derives authorization from verified claims server-side.
- Asset download signed URLs are workspace-scoped; a URL from workspace A cannot access workspace B's assets.
- Export packages are workspace-scoped; the export download intent validates workspace membership.

**PostgreSQL RLS policies (required for production tenancy):**
- Every generation table carrying `workspace_id` must have a row-level security policy that restricts SELECT, INSERT, UPDATE, and DELETE to the session's `current_setting('app.workspace_id')`. Policies cover `AssetVersionRecord`, `QualityReviewRecord`, `AssetQCAttemptRecord`, `RegenerationDraftRecord`, `ExportPackageRecord`, and their dependent tables.
- RLS is enforced via a restricted database role used by domain queries and commands, not the migration or superuser role.
- The application sets `app.workspace_id` from the verified `TenantContext` at the start of every request; a missing or empty setting fails closed (no rows visible).
- SQLite local-dev mode remains application-enforced with explicit `WHERE workspace_id = :wid` clauses; the RLS requirement applies only to the PostgreSQL production path.

**Denial audit logging (required):**
- Every cross-workspace denial (403 or masked 404) emits a structured audit event containing the denied workspace, the requesting actor, the target resource type and ID, the action attempted, and the stable denial code.
- Audit events are append-only and queryable by workspace or actor for incident investigation.
- Tests assert that audit records are created for each denial scenario below and that they do not leak the target resource's existence or content.

**Cross-Workspace browser tests (required):**
- An unauthorized Workspace slug returns 403. A separately authorized Workspace returns only its own rows, which may be an empty list.
- Asset detail returns 404 for a version_id belonging to another workspace.
- Review creation fails for a version not in the current workspace.
- Regeneration draft creation fails for a cross-workspace source version.
- Quality Feedback fails when `qc_attempt_id` belongs to another Workspace or another AssetVersion, including when paired with an otherwise valid source version.
- Review-summary regeneration fails atomically when any candidate version belongs to another Workspace; the response does not reveal which candidate existed.
- Export manifest and download intent fail for a package not in the current workspace.
- Download intent signed URL from workspace A cannot be used to fetch workspace B's blobs (server-enforced object key scoping).
- Each denial scenario above produces a corresponding audit record (verified by test).

### 10.3 Download Security

Per the integration contract §10 (File Seam):
- Binary content NEVER passes through Next.js.
- For display: `BrowserBlobTransferClient.downloadBlob()` → revocable `blob:` URL → `<img>` (see §6.3).
- For save-to-disk: `useGrantedDownload` → `validatedDownloadUrl()` → browser navigation.
- The adapter validates the exact storage origin; blob fetch uses `credentials: 'omit'`, `redirect: 'error'`, and abort support.
- Signed URLs are short-lived bearer capabilities — never logged, cached, or exposed to callers beyond immediate use.

### 10.4 Controlled Mock Provenance

Every asset and export carries `execution_mode`. The server stores `'byok'`, `'sandbox'`, or `'mock'` (per `ck_export_package_mode` constraint). The UI normalizes `'mock'` → `'controlled-mock'` for display via `ModeProvenance`:

- `mock_evidence` contains the immutable Adapter ID/version, `scenario_id`/scenario version, and related Run, RunItem, Provider Attempt, Operation, and Job evidence IDs. It is absent for non-Mock records rather than synthesized by the browser.
- Gallery cards: a `ModeProvenance` badge with `mode: 'controlled-mock'` (red `badge-error`) appears on every Mock asset, with the scenario available in accessible detail.
- Lightbox: prominent Mock badge plus Adapter, scenario, and related evidence IDs in the provenance section.
- Export manifest: `execution_mode` and `mock_evidence` are shown per entry; the package-level `execution_mode` confirms homogeneity and package evidence identifies the Export Job/Operation.
- **Mixed execution modes**: The server hard-rejects mixed selections (`export.py:110-115`). The UI blocks proactively at the ExportConfigStep — no "confirm anyway" dialog. This is a server invariant, not a soft warning.
- Controlled Mock still writes deterministic image bytes and a real ZIP through the same BlobStore/Job/Operation path. Only the external provider adapter is mocked; domain artifacts and lifecycle records are not.

---

## 11. Recovery Semantics

### 11.1 Refresh Safety

On page load:

1. Parse and canonicalize URL navigation state.
2. Fetch gallery with filters.
3. Fetch `selectedAssetId` directly when present; do not require it to be in loaded pages.
4. If `viewMode === 'lightbox'`, open lightbox.
5. If `viewMode === 'review'`, restore the matching queue snapshot or exhaust cursor pages until the active asset is located.
6. If `exportId` is set, resume export flow (see §8.4).
7. Before accepting a new review/export command, inspect recoverable pending actions and replay or reconcile them.

### 11.2 Review Mode Recovery

The live queue is in `ReviewModeProvider`; its durable session snapshot is `sessionStorage asset-review-queue:{slug}:{scopeHash}`:

- Restore ordered IDs, next cursor, current index, decisions, and session counters only when its canonical `scopeHash` matches the URL filters.
- Re-fetch the current asset’s detail and replace cached decisions with server effective-review truth where they differ.
- If the snapshot is absent, fetch cursor pages sequentially under the same scope until `asset` is found. A not-found/exhausted result exits review via canonicalizing `router.replace`.
- An unresolved pending review action is replayed with the same body/identity before keyboard input is enabled.

### 11.3 Export Operation Recovery

`GET /generation/exports/{id}` is authoritative for package and Operation identity. A browser cache is optional. A pending export-create action recovers the interval before the package ID is known. See §0.9 and §8.4.

### 11.4 Intent Recovery

`IntentTracker` uses injected `localStorage`; `deduplicateInflight()` is only an in-memory coalescer. Therefore the feature persists an indexable pending-action record containing the action identity and request body before sending.

- **Definitive success/problem:** clear the pending-action record; server replay has already enforced at-most-one domain mutation.
- **Network ambiguity/refresh:** retry the same body with the same action identity inside the safe window.
- **Reconciliation required:** keep the record, disable conflicting actions, and show the reconciliation UI.
- **User starts over:** only an explicit acknowledgement may dismiss the retained intent and create a new logical action.

Append-only semantics do not make duplicates acceptable. Review, regeneration-draft creation, export creation, and export download-intent creation all implement server replay before their UI is enabled.

---

## 12. Accessibility

### 12.1 Keyboard Navigation

- Gallery grid: Tab moves between cards; Enter/Space opens lightbox.
- Lightbox: Arrow keys for prev/next; Escape to close; A/C/X for review.
- Review mode: A/C/X/Space/← as documented; Escape to exit.
- Filter chips: Tab order includes all chips; Enter/Space toggles.
- Export modal: standard modal focus trap.

### 12.2 Screen Reader Support

- `AssetCard`: a `<button>` wrapping the thumbnail handles the preview action (Enter/Space opens lightbox); a separate `<input type="checkbox">` in the top corner handles multi-select. The button has `aria-label` describing the asset (kind, aspect ratio, provider, review status). The checkbox has `aria-label="Select asset {displayNumber}"`. This avoids the conflict where `role="img"` would swallow Enter/Space — the semantic elements provide native keyboard interaction.
- Review keyboard actions: `aria-live="assertive"` region announces the submitted review value.
- `OperationStatus`: `role="alert"` for failures, `aria-live="polite"` for progress.
- Filter chips: `role="checkbox"` with `aria-checked`.
- Selection count: `aria-live="polite"` region announces selection changes.

### 12.3 Focus Management

- Opening lightbox: focus trapped inside modal; on close, focus returns to the triggering card.
- Opening review mode: focus moves to the review canvas; on exit, focus returns to the gallery.
- Review advance: focus stays on the review canvas (no focus jump on asset change).

---

## 13. File Structure

```
web/
├── pages/w/[slug]/assets.tsx          # Page entry, URL state parsing, layout
├── hooks/domain/
│   ├── useDomainInfiniteQuery.ts       # Typed useSWRInfinite domain seam
│   └── useGrantedDownload.ts           # Consume an already-issued grant
├── lib/domain-client/
│   └── recoverable-action.ts           # Indexed pending actions over localStorage
├── components/assets/
│   ├── AssetsProvider.tsx              # Context: filters, selection, gallery data
│   ├── BatchRail.tsx                  # Run/product sidebar with run states
│   ├── GalleryZone.tsx                # Grid + filters + selection bar
│   ├── FilterBar.tsx                  # Filter chip groups
│   ├── AssetGrid.tsx                  # Responsive masonry grid
│   ├── AssetCard.tsx                  # Single thumbnail card
│   ├── SelectionBar.tsx               # Bulk action toolbar
│   ├── LightboxModal.tsx              # Full-size overlay
│   ├── AssetSidebar.tsx               # Metadata panel (shared lightbox/review)
│   ├── ReviewPanel.tsx                # Review + correction buttons with permission gating
│   ├── RegenerationPanel.tsx          # Four regeneration paths
│   ├── VersionHistory.tsx             # Lineage chain
│   ├── ReviewMode.tsx                 # Fullscreen keyboard review
│   ├── ReviewProgress.tsx             # Progress bar + reviewed/skipped counts
│   ├── ReviewSummary.tsx              # End-of-review summary + actions
│   ├── ExportFlow.tsx                 # Export modal wizard
│   └── ExportManifestView.tsx         # Manifest display
├── hooks/assets/
│   ├── useAssetsUrlState.ts           # URL ↔ state sync
│   ├── useAssetGallery.ts            # Infinite scroll gallery data
│   ├── useAssetActions.ts            # Permission-gated action resolution
│   ├── useAssetImageUrl.ts           # Adapter download + revocable blob URL
│   ├── useReviewMode.ts              # Persisted queue + keyboard/mutex/recovery
│   └── useExportFlow.ts              # Export wizard state machine
```

---

## 14. Acceptance Matrix

Each row maps to a §3.2 acceptance criterion from `v9-functional-alignment-acceptance.md`.

| # | Criterion | Component | Verification |
|---|-----------|-----------|-------------|
| 1 | Batch rail + aggregated gallery with filters | BatchRail + FilterBar + AssetGrid | Typed cursor pages load; server facets and provider/origin/mode/effective-review/aspect/QC/tag/contextual-conclusion filters narrow results; rail shows every authoritative Run state and item aggregate |
| 2 | Fullscreen review mode with A/C/X/Space/Left | ReviewMode | Enter review, press A → review created, advances; ← goes back; Escape exits to summary |
| 3 | Lightbox: combo tags, Provider, model, Seed, refs, QC, review, contextual conclusions, history, full prompt | LightboxModal + AssetSidebar | Every field is populated by the exact-version detail projection; lineage uses explicit parent/children |
| 4 | Single-asset: same recipe, new Seed, edit prompt, quality feedback | RegenerationPanel | Each button creates correct regeneration draft with correct `intent` enum value |
| 5 | Needs Changes ×2 → regenerate, Approved ×3 → supplement, contextual Loser skipping | ReviewSummary + server batch Module + generation preview | The server groups exact candidates and returns 2 assets per Needs Changes signature or 3 per Approved signature; next preview skips only Losers in explicit context and returns savings |
| 6 | Export: winner-only, approved-only, all-successful presets; content-hash dedup in manifest | ExportFlow + Worker + ExportManifestView | Server resolves each preset; deterministic ZIP has one payload per hash while retaining every logical manifest entry and full provenance |
| 7 | Controlled Mock → `controlled-mock` display; mixed modes blocked (server rejects) | ModeProvenance + provenance views + ExportConfigStep | Mock assets expose Adapter/scenario/related evidence as well as the red badge; export config blocks mixed selection |
| 8 | Server-authoritative permissions via action codes `review`/`regenerate`/`download` | useAssetActions | Buttons hidden/disabled per allowed_actions; no client role logic |
| 9 | Adapter-mediated blob display; signed URL navigation for save-to-disk | useAssetImageUrl + useGrantedDownload | Adapter tests cover origin/credentials/redirect/abort; media retry reuses action identity; stable domain problems and reconciliation render; img uses revoked `blob:` URL; signed URL is not persisted |
| 10 | Append-only effective review with correction and clear | ReviewPanel + server | Correction links head; cleared removes Approved readiness/filter membership; history remains intact |
| 11 | Refresh-safe navigation, queue, intent, and export recovery | URL + local/session storage + server GET | Restore later-page review position; replay lost review/export response once; package GET recovers Operation |
| 12 | Keyboard accessibility | All components | Tab order, focus trap, aria-live, screen reader announcements |
| 13 | Responsive layout (< 1024px) | CSS media queries | BatchRail collapses to ribbon; gallery fills width |
| 14 | No duplicate review: ref mutex + durable action identity + server replay | useReviewMode + review command | Rapid keys, network retry, and refresh/lost response append exactly one review |
| 15 | Cross-workspace isolation with RLS and denial audit | Browser/API tests + PostgreSQL RLS + audit log | Unauthorized slug is 403; Asset, QC Attempt, review-summary candidate, Export, and grant attacks fail closed without revealing existence; PostgreSQL RLS enforces row scoping; every denial emits a queryable audit record |
| 16 | Missing/uncommitted artifact failure | asset/export domain + BlobStore adapters | Media returns `asset_blob_unavailable`; export fails package/Operation and never publishes a grant |
| 17 | Export process recovery | Worker + package/Operation queries | Worker kill/restart at every commit checkpoint reuses one Job/Operation/package/ZIP; ambiguous commit is reconciled before retry |

---

## 15. Implementation Plan

### Phase 0: Contract Completion (hard gate before feature UI)

1. Add migrations for exact AssetVersion committed refs and export provenance/ZIP refs.
2. Make generation and Controlled Mock outputs commit real bytes through `BlobStore`; mark the RunItem successful only after commit.
3. Add exact-revision `BlobStore.open_committed`, strengthened `issue_download`, and `commit_generated_artifact` behavior to both S3 and local adapters.
4. Implement idempotent asset/export download-intent domain functions and routes with server-issued grants.
5. Add typed `AssetGalleryPage`, all server filters/facets including contextual conclusion, collection actions, Mock evidence, and the full `AssetVersionDetail` projection.
6. Implement review command replay, correction input/output, single-head effective-review projection, cleared readiness, and conflicts.
7. Add contextual experiment-conclusion projection and explicit `ExperimentContextRef`.
8. Make regeneration-draft creation replayable; add the atomic review-summary batch Interface; add enum/count input; and propagate source, intent, and count through Draft/snapshot/RunItem/Worker into child lineage.
9. Add server-side Loser exclusion and skipped-call/cost evidence to generation preview.
10. Add server-backed export selector resolution, `202 ExportAccepted` with Operation `Location`, package GET, and transactional Job enqueue.
11. Implement the export Worker’s deterministic ZIP, full immutable manifest, hash dedup, BlobStore commit, and Operation/package transitions.
12. Declare and verify the #48 dependency for run summaries, recovery preview, retry/resume/reconcile dispatch, and server-side recipe reuse; do not reimplement those domain Modules in #49.
13. Regenerate OpenAPI, operation map, BFF routes/allowlist, and TypeScript types.
14. Add contract/adapter/domain tests for every row in §0.10.

**Phase gate:** generated contracts are clean; all Phase 0 tests pass against local and fake adapters; no route returns placeholder domain truth.

### Phase 1: Browser Foundation

15. Modify `useDomainCommand.execute()` to return `CommandOutcome` while retaining state.
16. Add typed `useDomainInfiniteQuery`, shared domain cache-key helpers, and workspace-exact invalidation.
17. Extend `BrowserBlobTransferClient` with tested `downloadBlob`; add `useGrantedDownload`.
18. Add `RecoverableActionStore` over localStorage and startup replay/reconciliation for review, regeneration, media grants, and Export commands.
19. Create `useAssetsUrlState` with canonical filters/context/overlay/export parsing.
20. Create `AssetsProvider` and replace the page empty state with the two-zone shell.

**Phase gate:** hook tests prove cursor pagination, cache isolation, blob abort/revocation, and lost-response identity recovery.

### Phase 2: Gallery and Batch Rail

21. Create `useAssetGallery` over `useDomainInfiniteQuery`.
22. Create `useAssetImageUrl`, `AssetCard`, `AssetGrid`, and the sentinel.
23. Create the server-facet `FilterBar`, including effective review, aspect, QC, tags, experiment context, and contextual conclusion.
24. Create multi-selection and `SelectionBar` with collection export eligibility.
25. Create `BatchRail` over the #48 Interface with every Run/item state, action eligibility, `If-Match`, recovery previews, uncertain reconciliation, and conflict refresh.
26. Compose `GalleryZone` and responsive rail behavior.

**Phase gate:** browser tests traverse multiple cursor pages, restore filters, show actual image bytes, and execute only eligible run commands.

### Phase 3: Lightbox and Single-Asset Commands

27. Create `useAssetActions` from exact server action codes.
28. Create `AssetSidebar` for tags, full provenance including Mock Adapter/scenario/evidence, refs, QC, effective review/history, contextual conclusions, prompt, and recipe.
29. Create `VersionHistory` from explicit parent/children lineage.
30. Create `ReviewPanel` using head supersession, recoverable identity, and the synchronous mutex.
31. Create four-path `RegenerationPanel` with QC and requested-count validation.
32. Create `LightboxModal` with full-size blob media, stable problem/reconciliation recovery, and navigation.

**Phase gate:** correction/clear changes every dependent projection, each regeneration path creates the expected draft, and refresh restores the exact version.

### Phase 4: Keyboard Review

33. Create the persisted review queue snapshot and later-page restoration.
34. Create `useReviewMode` with permission gating, A/C/X/Space/Left, mutex, and replay/reconciliation.
35. Create `ReviewMode`, progress, focus management, and session summary.
36. Create Needs Changes ×2 and Approved ×3 actions over the server-owned review-summary batch Interface; expose contextual Loser exclusion in the resulting preview.

**Phase gate:** rapid keys, retry, lost response, and refresh append one review; ×2/×3 produces the exact requested RunItem count per signature.

### Phase 5: Export

37. Create `useExportFlow` with selector input, 202 outcome, Operation/package polling, pending-create recovery, and retry.
38. Create the three-step `ExportFlow` with context requirements and mixed-mode feedback.
39. Create `ExportManifestView` with full expandable provenance and dedup statistics.
40. Wire export grants to `useGrantedDownload`.

**Phase gate:** each preset creates a real downloadable deterministic ZIP; refresh works both before and after the package ID is known; unavailable blobs fail closed; Worker kill/restart and ambiguous object-commit checkpoints recover one package and ZIP.

### Phase 6: Integrated Acceptance

41. Complete cache invalidation, responsive CSS, loading/empty/error states, and Controlled Mock visual treatment.
42. Run keyboard and screen-reader accessibility checks.
43. Add authorized-empty vs unauthorized-403 and explicit cross-Workspace Asset, QC Attempt, batch candidate, Export, and grant browser/API tests; verify PostgreSQL RLS row scoping and assert denial audit records for every cross-workspace scenario.
44. Add API/Worker kill-restart and exact object-commit ambiguity tests for Export recovery.
45. Run the complete §14 matrix plus generated-contract drift checks.

**Completion criterion:** every §14 row passes with server-backed data; no client role/verdict/readiness logic, placeholder artifact, untyped asset page, or unrecoverable ambiguous command remains.

---

## 16. Resolved Product Defaults

- Review order is the current gallery order. Users choose `review=unreviewed` when they want unreviewed-first behavior.
- Multiple experiment contexts have no implicit Winner/Loser merge; the user must select one explicit context.
- Review-summary batch actions send exact session candidate IDs; the server groups by stable dimension signature and chooses the newest exact eligible version as the source.
- “Select all” means all loaded assets. Server presets cover all matching assets across unloaded pages.
