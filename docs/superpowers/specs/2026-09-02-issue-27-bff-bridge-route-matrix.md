# Issue #27 Domain Interface and BFF Bridge Contract

Status: Proposed design specification for Issue #27  
Date: 2026-09-02

## 1. Purpose, scope, and seam

This slice completes the production domain Interface needed by the New
Generation, Assets, and Creative Library pages and exposes it through the BFF
seam. It contains two distinct modules:

1. The **Domain Interface completion** is the deep module. It owns missing
   queries, commands, authorization, action eligibility, persistence changes,
   durable Jobs, BlobStore lifecycle, and response projections.
2. The **BFF Bridge** is a shallow Adapter. It owns only the allowlisted,
   same-origin transport work defined by ADR 0004: Workspace resolution,
   delegated identity, generated validation, forwarding, and response
   validation.

The implementation plan may stage these modules separately, but the slice is
complete only when both sets of completion gates in section 10 pass. UI page
implementation is outside this slice; wiring the generated `DomainGateway`
provider into the authenticated Workspace shell is part of the Bridge.

All browser-facing domain routes follow one mechanical mapping:

```text
Browser:  /api/domain/v1/w/{workspace_slug}/...
FastAPI:  /api/v1/...
```

`workspace_slug` is resolved by the BFF and is never forwarded as domain
identity. FastAPI derives Workspace, user, role, session, and authorization
epoch only from the delegated JWT.

Library and Generation routers are mounted only below `/api/v1`. The old
`/api/library/*` and `/api/generation/*` paths are not retained as aliases.
Neither the frozen static UI nor another production caller uses those paths.

## 2. Contract-wide rules

- Every route has a stable, explicit `operation_id`, exact success status, and
  explicit Pydantic request and success-response models.
- Every command that can create or mutate a persisted record, append evidence,
  create an Operation, or issue a capability is idempotent. Its OpenAPI entry
  carries `x-marketai-idempotent: true`.
- Mutable aggregates use one concurrency contract: detail responses expose an
  integer `revision` and `ETag: "<revision>"`; commands that depend on that
  state require the same value in `If-Match`. Expected revisions are not
  duplicated in JSON bodies. Missing `If-Match` returns
  `428 precondition_required`, malformed values return
  `400 invalid_precondition`, and stale values return
  `409 revision_conflict`. The normalized `If-Match` value participates in
  the idempotency request fingerprint.
- Long-running or recoverable commands return `202 Accepted`, the shared
  Operation response model, and a BFF-rewritable `Location` header pointing at
  that Operation. Synchronous resource creation returns `201 Created`; other
  successful synchronous queries and transitions return `200 OK`.
- Every unbounded collection query uses the shared `Page[T]` response
  `{ "items": T[], "next_cursor": string | null }`. `limit` defaults to 50,
  is bounded to 1..100, and invalid values fail with `422 validation_error`.
  The server-issued opaque cursor binds the Workspace, normalized filters, and
  last sort tuple. A malformed, tampered, or filter-mismatched cursor returns
  `400 invalid_cursor`. Routes declare their stable ordering below. Unknown
  query parameters fail closed.
- Query, body, and success-response validation are generated from the
  normalized OpenAPI document. Per-route hand-written allowlist glue is not
  permitted.
- Problem responses use the shared Problem Details shape and stable `code`
  values. The Web must not parse human-readable `detail` text.
- FastAPI projections that drive actions return server-calculated
  `allowed_actions: ActionEligibility[]`, ordered by stable action code. Each
  projection declares its `ActionCode` as an explicit OpenAPI string enum, not
  a free-form string. Each item is `{ "action": ActionCode, "allowed": boolean,
  "reason_code": string | null }`; denied actions require a stable reason
  code. The response includes both allowed and denied actions. The browser must
  not reconstruct domain eligibility from raw states or human-readable text.
- Binary content never passes through Next.js. The browser transfers bytes
  directly using short-lived capabilities after domain authorization.
- New v1 routes must use PostgreSQL records and production domain modules.
  They must not call the legacy `store` repository or process-local
  generation `manager`.

## 3. Creative Library Interface

The required #27 Library surface contains 20 operations: 16 existing domain
operations to remount and describe, plus four missing Interface operations.
The old multipart upload route is replaced by a JSON command that consumes a
finalized Upload Intent.

| operationId | Method | FastAPI path | Idempotent | Success | Query/body notes |
| --- | --- | --- | --- | --- | --- |
| `list_library_entry_types` | GET | `/api/v1/library/entry-types` | no | `200 LibraryEntryTypeList` | No query |
| `list_library_entries` | GET | `/api/v1/library/entries` | no | `200 Page[LibraryEntrySummary]` | `entry_type?`, `include_archived?`, `cursor?`, `limit?`; `(updated_at DESC, id DESC)` |
| `create_library_entry` | POST | `/api/v1/library/entries` | yes | `201 LibraryEntryDetail` | JSON create command |
| `get_library_entry` | GET | `/api/v1/library/entries/{entry_id}` | no | `200 LibraryEntryDetail` | Entry, current Draft/current Version, validation and action eligibility; returns `ETag` |
| `list_library_entry_versions` | GET | `/api/v1/library/entries/{entry_id}/versions` | no | `200 Page[LibraryVersionSummary]` | `cursor?`, `limit?`; `(created_at DESC, id DESC)` |
| `list_library_entry_events` | GET | `/api/v1/library/entries/{entry_id}/events` | no | `200 Page[LibraryEvent]` | `cursor?`, `limit?`; `(sequence DESC, id DESC)` append-only timeline |
| `save_library_draft` | PUT | `/api/v1/library/entries/{entry_id}/draft` | yes | `200 LibraryEntryDetail` | Complete Draft replacement plus `If-Match` |
| `validate_library_draft` | POST | `/api/v1/library/entries/{entry_id}/validate` | yes | `200 LibraryValidationResult` | Validates the Draft selected by `If-Match` |
| `publish_library_entry` | POST | `/api/v1/library/entries/{entry_id}/publish` | yes | `201 LibraryVersionDetail` | `If-Match` plus acknowledged warnings |
| `archive_library_entry` | POST | `/api/v1/library/entries/{entry_id}/archive` | yes | `200 LibraryEntryDetail` | `If-Match`; Entry transition |
| `bind_library_reference` | POST | `/api/v1/library/entries/{entry_id}/bind` | yes | `201 LibraryBinding` | Exact ReferenceAssetVersion; Draft selected by `If-Match` |
| `get_library_version` | GET | `/api/v1/library/versions/{version_id}` | no | `200 LibraryVersionDetail` | Immutable content, mutable lifecycle revision, provenance and action eligibility; returns `ETag` |
| `get_library_version_impact` | GET | `/api/v1/library/versions/{version_id}/impact` | no | `200 LibraryVersionImpact` | Rights/dependency impact and reason codes |
| `list_library_version_bindings` | GET | `/api/v1/library/versions/{version_id}/bindings` | no | `200 Page[LibraryBinding]` | `cursor?`, `limit?`; `(created_at DESC, id DESC)` |
| `withdraw_library_version` | POST | `/api/v1/library/versions/{version_id}/withdraw` | yes | `200 LibraryVersionDetail` | Owner-only; reason and `If-Match` required |
| `fork_library_version` | POST | `/api/v1/library/versions/{version_id}/fork` | yes | `201 LibraryEntryDetail` | Creates a new independent Library Draft |
| `clone_library_product` | POST | `/api/v1/library/versions/{version_id}/clone-product` | yes | `201 LibraryEntryDetail` | Safe defaults; optional content is explicitly selected |
| `apply_library_template` | POST | `/api/v1/library/versions/{template_version_id}/apply` | yes | `201 LibraryEntryDetail` | Deep-copies into a Product-local DimensionSchema Draft |
| `create_library_reference_object` | POST | `/api/v1/library/reference-objects` | yes | `202 Operation` | JSON command consuming an owned, finalized Upload Intent; never multipart |
| `create_library_reference_download_intent` | POST | `/api/v1/library/versions/{version_id}/download-intent` | yes | `201 DownloadIntent` | Resolves the authorized derivative server-side; browser supplies no object key |

### Reference upload sequence

Reference upload reuses the existing platform operations and does not add a
binary BFF route:

1. `create_upload_intent` with the Library reference purpose and metadata.
2. Browser uploads directly to the returned private-storage capability.
3. `finalize_upload`; browser polls the returned Operation.
4. After successful storage verification, `create_library_reference_object`
   atomically consumes the finalized intent, creates the quarantined Library
   reference identity and version, enqueues the gate/derivative Job, and
   returns its Operation.

The command in step 4 must verify the intent belongs to the same Workspace and
initiator, has the correct purpose, is finalized, has not been consumed by a
different reference command, and points at the verified committed object.
Intent consumption, reference creation, Job creation, and Operation creation
commit in one transaction. An exact idempotent replay returns the same
Operation and reference result; a different command attempting to consume the
same intent returns `409 upload_intent_consumed`.

The Worker runs privacy, rights, content-safety, and derivative gates. The
reference moves from `quarantined` to its domain terminal state; the Operation
independently follows the shared Operation state machine and settles as
`succeeded`, `failed`, or `uncertain`. The Operation `result_ref` identifies
the new ReferenceAssetVersion. A reference is selectable or downloadable only
after it reaches `released`.

### Explicitly deferred Library operation

`preview_library_template_reapply` may be added later at
`POST /api/v1/library/versions/{template_version_id}/reapply-preview`. It is
not required by the #27 Apply flow unless the UI also promises template-upgrade
diff/merge. Saving an accepted merge continues through `save_library_draft`.

## 4. Generation Draft and Preview Interface

| operationId | Method | FastAPI path | Idempotent | Success | Query/body notes |
| --- | --- | --- | --- | --- | --- |
| `list_generation_capabilities` | GET | `/api/v1/generation/capabilities` | no | `200 GenerationCapabilities` | Workspace-eligible Providers, models, modes, format limits and credential readiness |
| `list_generation_drafts` | GET | `/api/v1/generation/drafts` | no | `200 Page[GenerationDraftSummary]` | `state?`, `product_id?`, `cursor?`, `limit?`; `(updated_at DESC, id DESC)` |
| `create_generation_draft` | POST | `/api/v1/generation/drafts` | yes | `201 GenerationDraftDetail` | Persisted user intent; duplicate network submission must not create two Drafts |
| `get_generation_draft` | GET | `/api/v1/generation/drafts/{draft_id}` | no | `200 GenerationDraftDetail` | Full editable state, revision, provenance and current eligibility; returns `ETag` |
| `update_generation_draft` | PATCH | `/api/v1/generation/drafts/{draft_id}` | yes | `200 GenerationDraftDetail` | Partial JSON update plus `If-Match`; omitted fields remain unchanged |
| `create_generation_preview` | POST | `/api/v1/generation/drafts/{draft_id}/previews` | yes | `201 GenerationPreviewDetail` | `If-Match`; freezes exact versions, references, exclusions, final Prompt, capabilities, mode and maximum cost |
| `get_generation_preview` | GET | `/api/v1/generation/previews/{preview_id}` | no | `200 GenerationPreviewDetail` | Full frozen snapshot, validity, invalidation reason and submission state |

`create_generation_preview` must return the same full response projection as
`get_generation_preview`; the existing hash-only response is insufficient for
the confirmation UI.

The capabilities query is Workspace-scoped. It must not merely expose the
process-global Provider registry: entitlement, mode, credential availability,
format limits, and Provider/model compatibility are resolved by FastAPI.

## 5. Generation Run and recovery Interface

| operationId | Method | FastAPI path | Idempotent | Success | Query/body notes |
| --- | --- | --- | --- | --- | --- |
| `list_generation_runs` | GET | `/api/v1/generation/runs` | no | `200 Page[GenerationRunSummary]` | `product_id?`, `state?`, `execution_mode?`, `cursor?`, `limit?`; `(created_at DESC, id DESC)` |
| `submit_generation` | POST | `/api/v1/generation/runs` | yes | `202 Operation` | Fresh Preview ID/hash plus Draft `If-Match` |
| `get_generation_run` | GET | `/api/v1/generation/runs/{run_id}` | no | `200 GenerationRunDetail` | Run summary, counts, frozen recipe reference, commands, Operation and `allowed_actions`; returns `ETag` |
| `list_generation_run_items` | GET | `/api/v1/generation/runs/{run_id}/items` | no | `200 Page[GenerationRunItemSummary]` | `state?`, `cursor?`, `limit?`; `(ordinal ASC, id ASC)`; each row includes latest Attempt/QC/Asset summary and `allowed_actions` |
| `get_generation_run_item` | GET | `/api/v1/generation/runs/{run_id}/items/{run_item_id}` | no | `200 GenerationRunItemDetail` | Full Provider Attempt events, settlement/reconciliation, QC evidence, cost, AssetVersion binding and `allowed_actions` |
| `create_generation_recovery_preview` | POST | `/api/v1/generation/runs/{run_id}/recovery-previews` | yes | `201 GenerationRecoveryPreview` | `If-Match`; `retry_failed_items` or `resume_paused_items`, exact targets and maximum additional cost |
| `cancel_generation_run` | POST | `/api/v1/generation/runs/{run_id}/cancel` | yes | `202 Operation` | Production command plus `If-Match` |
| `retry_failed_generation_items` | POST | `/api/v1/generation/runs/{run_id}/retry-failed-items` | yes | `202 Operation` | Fresh recovery Preview ID/hash plus `If-Match` |
| `resume_paused_generation_items` | POST | `/api/v1/generation/runs/{run_id}/resume-paused-items` | yes | `202 Operation` | Fresh recovery Preview ID/hash plus `If-Match` |
| `reconcile_generation_run` | POST | `/api/v1/generation/runs/{run_id}/reconcile` | yes | `202 Operation` | Exact uncertain targets, optional operator note, and `If-Match`; no browser-authored provider evidence |

`submit_generation` and the four Run mutation commands (cancel, retry, resume,
and reconcile) are production domain implementations, not thin handlers over
legacy code. Each appends a `GenerationRunCommandRecord`, binds it to one
Operation, uses compare-and-swap on the Run revision, and enqueues only valid
item work. `create_generation_recovery_preview` instead creates an immutable,
idempotent snapshot and does not append a Run command or advance the Run.

Cancel immediately prevents undispatched queued work from starting. For an
in-flight Provider attempt, the Worker requests Provider cancellation only
when the Provider Adapter supports it. The item becomes cancelled only after
authoritative confirmation; otherwise it becomes `uncertain` and requires
reconciliation. Cancellation must never manufacture a successful settlement
or erase Provider events.

The reconcile request selects uncertain targets but does not carry
authoritative Provider evidence. The Worker obtains that evidence through the
Provider Adapter, appends it to the matching Attempt, and settles each target
with compare-and-swap. An operator note is audit context only. `uncertain`
items are never retried through the ordinary failed-item path.

## 6. Assets Interface

The read Interface is deliberately deep. FastAPI assembles the AssetVersion
detail projection so the browser does not join raw persistence records or
reimplement action eligibility.

| operationId | Method | FastAPI path | Idempotent | Success | Query/body notes |
| --- | --- | --- | --- | --- | --- |
| `list_assets` | GET | `/api/v1/generation/assets` | no | `200 Page[AssetSummary]` | Workspace gallery with `product_id?`, `run_id?`, `provider?`, `execution_mode?`, `review_value?`, `qc_state?`, `origin_kind?`, `cursor?`, `limit?`; `(created_at DESC, version_id DESC)` |
| `get_asset_version_detail` | GET | `/api/v1/generation/asset-versions/{version_id}` | no | `200 AssetVersionDetail` | Asset identity, sibling/parent/child lineage, RunItem, recipe, cost, QC, reviews, provenance and `allowed_actions` |
| `create_asset_review` | POST | `/api/v1/generation/asset-versions/{version_id}/reviews` | yes | `201 AssetReview` | Append-only exact-version decision and optional correction linkage |
| `create_asset_regeneration_draft` | POST | `/api/v1/generation/asset-versions/{version_id}/regeneration-drafts` | yes | `201 GenerationDraftDetail` | `same_recipe`, `new_seed`, `edit_prompt`, or `quality_feedback` |
| `create_asset_download_intent` | POST | `/api/v1/generation/asset-versions/{version_id}/download-intent` | yes | `201 DownloadIntent` | Resolves the committed AssetVersion object server-side |

`skip` is browser navigation and creates no domain record. Review decisions use
the stable enum `approved`, `needs_changes`, `rejected`, or `cleared`. Undo is
not a delete: `cleared` requires `supersedes_review_id` and appends a Review
linked to the current effective Review on the exact AssetVersion. A corrected
non-clear decision may also supersede the current Review. When an effective
Review already exists, any new decision must supersede it. The persistence model
therefore requires a nullable self-reference and a uniqueness constraint that
prevents two Reviews from superseding the same prior Review. A stale or
cross-version correction returns `409 review_conflict`.

Regeneration by `quality_feedback` requires `qc_attempt_id`. FastAPI verifies
that exact QC Attempt belongs to the same Workspace and AssetVersion and uses
that exact evidence when creating the Draft; it must not silently select some
other attempt. A mismatch returns `409 qc_attempt_mismatch`.

The AssetVersion detail response includes review and QC history needed by the
lightbox. A separate `list_reviews` route is unnecessary for #27 unless those
histories become independently paginated.

### Asset BlobStore invariant

A produced AssetVersion is successful and gallery-visible only after the
Worker has written its bytes to staging, committed them through the BlobStore
Adapter, verified content hash and byte size, and persisted the committed blob
key. AssetVersion persistence, RunItem success, and the Asset binding must not
claim success before that commit. Download and Export commands resolve this
server-owned committed reference and never accept an object key from the
browser. A legacy or damaged row without a valid committed reference is not
downloadable or exportable and reports `asset_blob_unavailable` through
`allowed_actions` and Problem Details.

## 7. Export Interface

| operationId | Method | FastAPI path | Idempotent | Success | Query/body notes |
| --- | --- | --- | --- | --- | --- |
| `create_export_package` | POST | `/api/v1/generation/exports` | yes | `202 Operation` | Freezes exact AssetVersion selection |
| `get_export_manifest` | GET | `/api/v1/generation/exports/{export_package_id}/manifest` | no | `200 ExportManifest` | Available only after ready; complete deterministic provenance manifest |
| `create_export_download_intent` | POST | `/api/v1/generation/exports/{export_package_id}/download-intent` | yes | `201 DownloadIntent` | Resolves package object server-side and returns a short-lived capability |

The existing `create_export_package` implementation is not complete merely
because it inserts an `ExportPackageRecord`. The command must enqueue the
durable export job, advance its Operation, create and commit the ZIP, record
the hash and size, and settle as ready/failed/uncertain without duplicate
packages or capabilities.

## 8. Counts and scope

The required Issue #27 addition contains:

- 20 Creative Library operations;
- 22 Generation/Run/Assets operations;
- 3 Export operations;
- 45 Issue #27 operations in total.

Existing platform and entitlement operations, including identity, Operation
polling, Upload Intent/finalize, credential management, Usage and Provider Cost
queries, remain registered but are not included in this count.

The original proposed matrix contained 30 operations, not 31. It omitted the
Library Apply and upload-consumption steps, Draft and Preview recovery,
Workspace capability resolution, Run discovery and recovery commands, detailed
RunItem evidence, secure Asset media access, and a sufficiently deep
AssetVersion projection.

## 9. Generator requirements

Remounting routes below `/api/v1` is necessary but not sufficient. Before the
first query-bearing Library or Generation route is registered:

1. `generate_openapi.py` continues to extract only `/api/v1/*` operations.
2. `generate_allowlist.ts` must generate runtime query validators from OpenAPI
   instead of emitting `querySchema: null` for every operation.
3. The BFF query decoder must preserve repeated values with
   `URLSearchParams.getAll`; it must not collapse `URLSearchParams` through
   `Object.fromEntries`. Boolean, integer, enum, repeated, defaulted, cursor and
   bounded page-size parameters are parsed and serialized deterministically.
4. Generated tests cover zero, one, and repeated values, duplicate scalar
   values, malformed coercions, unknown parameters, cursor/filter mismatch,
   and default/minimum/maximum page sizes.
5. Every operation receives an explicit fail-closed policy entry containing
   its BFF pattern, body/response limits and conditional-header behavior.
   Operations using the revision contract explicitly allow `If-Match`; other
   operations do not forward arbitrary conditional headers.
6. The OpenAPI document generates typed path builders, query/body/result types,
   and the corresponding `DomainGateway` method for every operation. The
   authenticated Workspace shell provides that gateway; callers do not add
   hand-written transport methods.
7. `generate_all.sh` regenerates all downstream artifacts, and CI verifies both
   normalized OpenAPI drift and generated-artifact drift.
8. Contract tests prove that unknown methods, paths, query parameters, bodies,
   Workspace identifiers, object keys and operation IDs fail closed before
   FastAPI forwarding.

## 10. Completion gates

### Domain Interface readiness

The deep Domain Interface module is complete only when:

- every route has its production query or command implementation, rather than
  a handler over the legacy `store` or process-local generation `manager`;
- required persistence changes exist, including Review correction linkage,
  command/Operation binding, and committed Asset blob identity;
- Reference processing, Run recovery, reconciliation, Asset output persistence,
  and Export packaging use durable Jobs and settle their Operations;
- Reference and Asset success states cannot precede verified BlobStore commit;
- all persisted commands replay safely with one user intent;
- action eligibility and denied reason codes come from FastAPI projections;
- cross-Workspace IDs, mismatched Asset/Version/QC relationships, stale
  revisions, stale Review corrections, and forbidden Owner actions fail
  server-side; and
- Python unit, domain integration, migration, Worker, concurrency, and failure
  recovery tests pass.

### BFF Bridge readiness

The shallow BFF Adapter is complete only when:

- every required operation above is represented by an explicit OpenAPI
  operation and generated BFF entry;
- the generated typed `DomainGateway` exposes every operation and its provider
  is wired into `WorkspaceLayout`;
- no Workspace domain caller uses a legacy unversioned path or hand-written
  transport call;
- all query-bearing operations pass generated query validation;
- repeated query parameters survive the BFF seam without lossy collapse;
- required `If-Match` headers are forwarded only for declared operations and
  participate in request fingerprinting;
- reference and Asset bytes bypass Next.js while authorization remains in the
  domain Interface;
- response validation covers every documented success status;
- OpenAPI, policy, generated artifacts, Web tests, and the relevant Playwright
  bridge scenarios pass without drift; and
- a combined integration test proves a delegated Workspace request crosses the
  generated gateway, BFF Adapter, Domain Interface, Operation polling, and
  signed capability flow without exposing Workspace identity or object keys.
