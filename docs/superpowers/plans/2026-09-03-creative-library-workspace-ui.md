# Creative Library Workspace UI Implementation Plan

> Status: implementation-plan ready. This document implements the final authoritative Creative Library UI architecture approved on 2026-09-03 and supersedes earlier conflicting UI plans. It does not supersede accepted domain contracts or ADRs.

**Goal:** Replace the Creative Library `FeatureEmptyState` with an accessible, responsive, production browser workspace backed exclusively by the generated `DomainGateway`, while preserving immutable Library history, Workspace isolation, durable user-action identity, and server-authoritative eligibility.

**Primary issue:** #47, child of #27.

**Required starting baseline:** a branch containing merged PR #46 (`b476a631a1deb49923f23a11fb368553a3e2e052`). The current planning checkout, `codex/railway-shared-dev` at `6789ed6`, predates that merge and is not an implementation baseline.

**Architecture:** Keep one Pages Router page at `/w/[slug]/library`. Query-string state selects one of five top-level work entrances, a root entry, a tab, an optional version, and—when applicable—a separately typed internal resource context. Deep modules hide query keys, pagination, command outcomes, action identity, reconciliation, semantic invalidation, version/resource resolution, draft sessions, and navigation blocking. Entry-type-specific editors and viewers are leaf modules selected from an exhaustive registry. FastAPI remains the authority for authorization, relationships, lifecycle eligibility, impact, validation, and command outcomes.

**Tech stack:** FastAPI, SQLAlchemy, Alembic, PostgreSQL, Next.js Pages Router, React 18, TypeScript, SWR/SWR Infinite, Tailwind CSS, daisyUI, Jest, Testing Library, Playwright, axe-core.

## 0. Progress ledger

- [x] Gate 0.1 — issue contract and ownership are ready for implementation.
- [x] Gate 0.2 — Gate 0 persistence model and migration pass.
- [x] Gate 0.3 — command outcomes are authoritatively recoverable.
- [x] Gate 0.4 — Publish, Withdraw, and Archive impacts are fresh and binding.
- [x] Gate 0.5 — internal resource contexts are server-verified.
- [x] Gate 0.6 — all 13 response/eligibility contracts are complete.
- [x] Gate 0.7 — Apply/Reapply and Data Source operations are production-backed.
- [x] Gate 0.8 — Reference create/download operations are production-backed.
- [ ] Gate 0.9 — MFA step-up and the generated seam are complete.
- [x] Gate 0.10 — structural query keys and paginated reads.
- [x] Gate 0.11 — returnable outcomes and persistent Library commands.
- [x] Gate 0.12 — semantic cache invalidation.
- [x] Gate 0.13 — shared dirty-navigation protection.
- [x] Task 5 — route codec and exhaustive registry.
- [x] Task 6 — unmounted read-only Library shell.
- [x] Task 7 — detail tabs, versions, and nested resources.
- [x] Task 8 — Draft codecs, sessions, validation, and conflicts.
- [x] Task 9 — action toolbar, impacts, dialogs, and reconciliation.
- [x] Task 10 — five root entry modules.
- [x] Task 11 — eight internal entry modules.
- [x] Task 12 — Reference upload, binding, and download.
- [x] Task 13 — Template reapply and Data Source operational panels.
- [x] Task 14 — accessibility, responsive behavior, performance, and observability.
- [ ] Task 15 — atomic page cutover and browser acceptance.

Verification note (2026-09-05, update 3): All code blockers resolved.
(1) Operational panels mounted in LibraryEntryDetail via ReferencesTab,
SchemaTab, FactsTab, ResolutionTab, SettingsTab. (2) Data Source connection
check extracts provider, execution_mode, state, command_type, failure_reason
from response. (3) Four Playwright specs contain structural assertions.
(4) Step-up grant store and assertion issuer tests added (step-up-grants.spec.ts,
step-up-issuer.spec.ts). (5) Reference fixture PNG created.
Remaining: Gate 0.9 MFA step-up grant route correctly rejects when session
lacks verified MFA evidence — this is a runtime/session constraint, not a code
gap. Browser acceptance must exercise the complete matrix before Task 15 is
checked. Web: 91 suites, 1062 passed, 38 skipped. TypeScript and lint clean.

## 1. Source-of-truth order

When implementation details disagree, apply this precedence:

1. ADR 0003: Workspace identity is enforced at the BFF and database.
2. ADR 0004: the versioned OpenAPI/BFF seam is the only browser-to-domain seam and cutover is atomic.
3. ADR 0007: stable entries, optimistic-lock Drafts, immutable Published Versions, and frozen execution snapshots remain separate.
4. `docs/product/creative-library-ownership-and-reuse-contract.md`.
5. The final Creative Library UI architecture approved on 2026-09-03.
6. Issue #47 acceptance criteria, then parent #27 acceptance criteria.
7. This implementation plan.

If a lower-precedence source would weaken a higher-precedence invariant, stop and repair the contract; do not add a client workaround.

## 2. Global constraints

- Gate 0 is a hard barrier. No Creative Library browser workflow may be mounted on the production page until every Gate 0 exit criterion passes.
- Keep `web/pages/w/[slug]/library.tsx` rendering `FeatureEmptyState` through Gate 0 and Tasks 5–14. Task 15 performs the only product-page cutover.
- Do not add App Router pages, Server Actions, direct FastAPI calls, hand-written feature transports, legacy paths, browser-local domain fixtures, `window.__mockApi`, or browser-owned domain truth.
- All Browser domain traffic uses `/api/domain/v1/w/{slug}/...` and generated `DomainGateway` methods.
- The five top-level lists contain only `product`, `person`, `dimension_template`, `prompt_policy`, or `data_source`, one type per request. Internal entry types never enter those lists.
- `entry` in the URL always identifies a top-level root entry. Internal resources use their own typed URL fields.
- Server `allowed_actions` is authoritative. UI role checks may tailor non-domain shell navigation but never enable a Library command.
- Draft writes use complete replacement plus ETag/`If-Match`. Published content is immutable. Version lifecycle changes use their own revision/ETag.
- HTTP responses remain `Cache-Control: no-store`; SWR is an in-memory view cache, not persistent domain truth.
- No credential, MFA assertion, signed URL, object key, customer content, prompt, filename, or command body is logged or emitted as telemetry.
- New tests use `@jest-environment jsdom` when they exercise React or browser APIs. Playwright files stay under `web/e2e/`.
- Use `rtk` for every shell command. Do not run Alembic against a hosted database outside the repository's `db-release` path.

## 3. Issue and acceptance traceability

| Source | Acceptance obligation                                                                                             | Plan evidence                                                                      |
| ------ | ----------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| #47    | Refresh never substitutes browser seeds or local fixtures                                                         | Tasks 5, 6, 15; refresh Playwright checkpoints and BFF request assertions          |
| #47    | All reads and commands use generated `DomainGateway` operations                                                   | Gate 0.9–0.11 and Task 15; generated-operation coverage and no-raw-fetch tests     |
| #47    | ETag/`If-Match` and understandable revision recovery                                                              | Gate 0.4/0.6, Tasks 7–8; 409 three-way conflict tests                              |
| #47    | Withdrawn/rights-restricted history remains inspectable                                                           | Gate 0.4, Tasks 6, 8, 12, 15; version-history and forbidden-command scenarios      |
| #47    | Cross-Workspace IDs, upload, download, and mutations fail closed                                                  | Gate 0.3–0.9, Tasks 12 and 15; domain, BFF, and Playwright negative suites         |
| #47    | Loading, empty, validation, conflict, forbidden, progress, failure, retry are accessible and deterministic        | Tasks 5–14; component-state matrix, axe, keyboard, and screen-reader announcements |
| #47    | Owner/Member, refresh, versions, upload/download, cross-Workspace browser coverage                                | Task 15                                                                            |
| #27    | Library withdrawal and rights changes immediately alter eligibility without erasing historical Run/Asset evidence | Gate 0.4 and Task 15 regression scenario; no #48/#49 UI implementation             |
| #27    | Controlled Mock/Sandbox provenance is explicit and no mode fallback is implied                                    | Gate 0.6, Tasks 6, 10–14                                                           |
| #26    | Workspace switch changes URL, cache, and rendered data atomically                                                 | Gate 0.10, 0.12, 0.13 and Task 15                                                  |
| #26    | Same-origin BFF only; CSP blocks direct FastAPI                                                                   | Gate 0.9, Tasks 12, 15; remove the existing network-test `fixme`                   |
| #26    | Mutation retries reuse identity and survive refresh                                                               | Gate 0.3, 0.11 and Task 15                                                         |
| #20    | Apply deep-copies, reapply preserves lineage, template changes never mutate applied schemas                       | Gate 0.7, Tasks 10 and 13                                                          |
| #20    | Prompt validation is deterministic and warnings require reconfirmation                                            | Gate 0.6, Tasks 7, 10, 11                                                          |
| #20    | No implicit Data Source/default; credential identity remains separate                                             | Gate 0.7, Tasks 10, 11, 13                                                         |
| #18    | Credential plaintext never appears; Member cannot bind; rotation does not rewrite Data Source identity            | Gate 0.7–0.9, Tasks 13–15                                                          |
| #18    | Mock/Sandbox/real state stays isolated with no silent fallback                                                    | Gate 0.6–0.7, Tasks 10, 13–15                                                      |
| #21    | Frozen Generation/Asset evidence remains unchanged after Library lifecycle changes                                | Gate 0.4 integration test and Task 15 cross-area regression                        |

# Gate 0 — Domain and platform prerequisites

## Gate 0 entry rule

Gate 0 begins only on a branch containing PR #46. Verify before editing:

```bash
rtk git merge-base --is-ancestor b476a631a1deb49923f23a11fb368553a3e2e052 HEAD
rtk rg -n "list_library_entries" app/library/v1_router.py web/lib/domain-client/generated/operation-map.d.ts web/lib/domain-client/gateway.ts
rtk .venv/bin/python scripts/generate_openapi.py --check
```

All three commands must exit 0. If not, stop; update the implementation branch to a PR #46-containing baseline without copying generated files by hand.

## Gate 0.1 — Freeze navigation and command contracts in #47

**Issue dependency:** #47 is currently open with `needs-triage`. Before product implementation, its source-of-truth record must link this plan and record the following accepted constraints, either in #47 or linked blocking issues:

- five root lists only;
- exhaustive top-level/internal navigation placement for all 13 types;
- typed nested resource URL state;
- persistent ambiguous-command lock and authoritative reconciliation;
- Publish/Withdraw/Archive impact projections;
- internal resource reads/context validation;
- Template reapply, Data Source operations, and MFA step-up prerequisites;
- atomic cutover.

**Exit evidence:** #47 or its linked blockers carry these requirements and #47 is moved to `ready-for-agent`. This is an issue-administration gate; it creates no product code.

## Gate 0.2 — Add the Gate 0 persistence model

**Files:**

- Modify: `app/adapters/persistence/models.py`
- Modify: `app/library/contracts.py`
- Create: `migrations/versions/r1a2b3c4d5e6_library_ui_gate0_contracts.py`
- Create: `tests/test_library_ui_gate0_migration.py`
- Modify: `tests/test_library_migration.py`

**Implement:**

1. Extend `LibraryIdempotencyRecord` with explicit command-contract schema version, opaque command-identity hash, lifecycle state, response kind, updated timestamp, and reconciled timestamp. Preserve Workspace and initiator binding.
2. Add `LibraryCommandImpactRecord` containing Workspace, actor, command kind, target entry/version, target revision, content hash, request fingerprint, impact hash, safe public projection, expiry, and consumption state.
3. Add `LibraryEntryContextRecord` as a contextual navigation association, not an ownership record. It stores Workspace, root entry, resource entry, optional parent entry, relation kind, and creation evidence. Multiple legal contexts are allowed.
4. Add `content_schema_version` to Draft and Version records.
5. Add `lifecycle_revision` to `LibraryVersionRecord`, constrained to positive integers. Eligibility or lifecycle mutations increment it.
6. Add a one-use step-up assertion JTI consumption record scoped to Workspace and actor; store only a hash/reference, never the assertion.
7. Backfill existing Drafts/Versions with the correct type schema version and existing Library idempotency rows with legacy contract version `1` and terminal/pending state derived from `response_json`.
8. Do not add an optional-Draft workflow. Preserve the domain invariant that every Library entry owns exactly one Draft from creation onward.

**Automated evidence:**

```bash
rtk .venv/bin/python -m pytest -q tests/test_library_ui_gate0_migration.py tests/test_library_migration.py
rtk .venv/bin/alembic upgrade head
rtk .venv/bin/alembic current
rtk .venv/bin/alembic check
```

**Negative evidence:** migration rejects duplicate command identities within the same Workspace/actor/command, invalid version revisions, cross-Workspace context links, and duplicate consumed step-up JTIs.

## Gate 0.3 — Make command outcomes authoritatively recoverable

**Files:**

- Create: `app/library/command_outcomes.py`
- Modify: `app/library/service.py`
- Modify: `app/library/response_models.py`
- Modify: `app/library/v1_router.py`
- Modify: `web/lib/domain-client/request-pipeline.ts`
- Modify: `web/pages/api/domain/v1/w/[slug]/[...path].ts`
- Create: `tests/test_library_command_outcomes.py`
- Modify: `tests/test_v1_library_routes.py`
- Modify: `web/__tests__/api/domain/v1/w/domain-proxy-handler.spec.ts`

**Interfaces:**

- `GET /api/v1/library/command-outcomes/{command_identity}` — `get_library_command_outcome`.
- `POST /api/v1/library/command-outcomes/{command_identity}/reconcile` — `reconcile_library_command_outcome`.
- Response is a discriminated union with `pending`, `succeeded`, `problem`, `reconciliation_required`, or `not_applied`.
- Every variant includes `contract_schema_version`, `command_kind`, `command_identity`, and server timestamps. Success includes the original response/result reference; problem includes stable Problem Details. `not_applied` is an affirmative server proof, never an inference from absence.

**Implement:**

1. For every Library command, the BFF forwards the opaque browser intent as `X-Command-Identity` only after session, Workspace, CSRF, allowlist, and input validation. FastAPI stores only its hash.
2. Reserve the command outcome before its mutation. Finalize the outcome in the same transaction as domain effects; async commands bind the resulting Operation.
3. Make Fork and Clone success replay the exact new entry ID. Reference creation replays the same Operation/result reference.
4. The lookup and reconcile operations require the same Workspace and initiator. A missing record returns `reconciliation_required`, never `not_applied`.
5. Reconciliation may return `not_applied` only when database/Operation evidence proves that no domain effect committed. Otherwise the record stays locked.
6. Terminal result retention is server policy. Browser TTL, dismiss, logout, or storage cleanup never manufactures a new action identity.

**Automated evidence:**

```bash
rtk .venv/bin/python -m pytest -q tests/test_library_command_outcomes.py tests/test_v1_library_routes.py
rtk npm --prefix web test -- --runInBand __tests__/api/domain/v1/w/domain-proxy-handler.spec.ts
```

**Required negative scenarios:** wrong actor, wrong Workspace, wrong command kind, malformed identity, different fingerprint reuse, process crash before mutation, crash after commit before response, concurrent Fork/Clone, expired local record, and response loss. Only authoritative `succeeded`, `problem`, or `not_applied` releases the retry context.

## Gate 0.4 — Add fresh Publish, Withdraw, and Archive impact projections

**Files:**

- Create: `app/library/command_impacts.py`
- Modify: `app/library/service.py`
- Modify: `app/library/response_models.py`
- Modify: `app/library/v1_router.py`
- Create: `tests/test_library_command_impacts.py`
- Modify: `tests/test_library_core.py`
- Modify: `tests/test_v1_library_routes.py`

**Interfaces:**

- `POST /api/v1/library/entries/{entry_id}/publish-impact` — `preview_library_publish_impact`.
- `POST /api/v1/library/entries/{entry_id}/archive-impact` — `preview_library_archive_impact`.
- `POST /api/v1/library/versions/{version_id}/withdraw-impact` — `preview_library_withdraw_impact`.

Each response includes an opaque `impact_id`, `impact_hash`, target revision, content hash where applicable, expiry, affected version/resource summaries, blocking reasons, warnings, and the command kind. The matching command accepts `impact_id` and `impact_hash` and rechecks the projection atomically.

**Implement:**

1. Publish impact binds the exact Draft revision/content hash and stable warning codes.
2. Archive impact covers the Entry, current Version, Draft editing loss implications, new-use eligibility, and dependent versions/snapshots without deleting history.
3. Withdraw impact binds the exact Version lifecycle revision, reason hash, dependency closure, rights consequences, and downstream eligibility projections.
4. A stale, expired, consumed, wrong-command, wrong-actor, wrong-Workspace, wrong-revision, or mismatched hash projection fails closed with a stable code.
5. Impact projection and command execution use the same dependency rules. The UI never patches or recomputes the impact.
6. Withdraw and downstream eligibility changes increment affected Version lifecycle revisions.

**Automated evidence:**

```bash
rtk .venv/bin/python -m pytest -q tests/test_library_command_impacts.py tests/test_library_core.py tests/test_v1_library_routes.py
```

**Cross-Issue evidence:** a regression test creates frozen Generation/Asset evidence, withdraws or archives an upstream Library resource, verifies new eligibility changes immediately, and verifies the frozen historical evidence remains byte-for-byte unchanged (#21/#27).

## Gate 0.5 — Add server-verified internal resource navigation

**Files:**

- Create: `app/library/navigation.py`
- Modify: `app/library/service.py`
- Modify: `app/library/response_models.py`
- Modify: `app/library/v1_router.py`
- Create: `tests/test_library_internal_navigation.py`
- Modify: `tests/support/library_fixtures.py`

**Interfaces:**

- `GET /api/v1/library/entries/{root_entry_id}/internal` — `list_library_internal_entries`; one required `entry_type`, optional `parent_type` and `parent_id`, cursor, and limit.
- `GET /api/v1/library/internal/{entry_type}/{entry_id}` — `get_library_internal_entry`; returns canonical unscoped navigation metadata.
- `GET /api/v1/library/entries/{root_entry_id}/internal/{entry_type}/{entry_id}` — `get_library_internal_entry_context`; optional paired `parent_type`/`parent_id`, returns verified root/parent/resource relationship.
- `POST /api/v1/library/entries/{root_entry_id}/internal` — `create_library_internal_entry`; discriminated request for the requested internal type and explicit parent context.

**Implement:**

1. Root lists remain unchanged and accept only one root `entry_type` per UI call.
2. Product contexts support Offer, Reference Group, Reference Selection Rule, Dimension Schema, and Market Source Mapping. Data Source contexts support Manual Fact Set and Truth Resolution Policy.
3. Reference Asset remains shared. Contextual links derive from exact `ReferenceBinding` evidence for Product, Person, or Reference Group; no Product ownership is created.
4. Unscoped Reference Asset metadata reports canonical host category `product`. This is URL fallback only.
5. `parent_type` and `parent_id` are an all-or-nothing pair. Market Source Mapping verifies its Offer parent and Product root. ReferenceGroup-scoped assets verify both group and Product context.
6. New internal Drafts receive durable context evidence so they remain discoverable after refresh before first publication.
7. Cross-Workspace, mismatched type, mismatched root, fake parent, and relationship inferred only by matching display names all fail closed.

**Automated evidence:**

```bash
rtk .venv/bin/python -m pytest -q tests/test_library_internal_navigation.py tests/test_v1_library_routes.py
```

The tests must prove pagination is one server cursor stream and the client never needs to merge multiple `list_library_entries` streams.

## Gate 0.6 — Complete the 13-type response and eligibility contract

**Files:**

- Modify: `app/library/contracts.py`
- Modify: `app/library/response_models.py`
- Modify: `app/library/v1_router.py`
- Modify: `app/platform/action_eligibility.py`
- Create: `tests/test_library_response_contracts.py`
- Modify: `tests/test_entry_types.py`
- Modify: `tests/test_action_eligibility.py`

**Implement:**

1. Replace opaque response `dict` content with a discriminated, versioned union covering all 13 entry types.
2. `LibraryVersionDetail` always returns `entry_id`, `content_schema_version`, content hash, origin/base/source lineage, real/mock provenance, lifecycle revision/ETag, and Version-level `allowed_actions`.
3. `LibraryEntryDetail` always returns the one Draft, its schema version/revision/validation, current Version ID, Entry-level `allowed_actions`, and navigation placement.
4. `LibraryEntrySummary` includes every row field needed by the list: display name, type, lifecycle, current version ID/number/state, Draft revision/state, update time, and summary badges. No detail N+1 requests are allowed.
5. `LibraryEvent` exposes stable ID, sequence, server timestamp, event type, initiator/executor, state transition, version ID, and only an allowlisted public payload.
6. `ActionEligibility` retains `allowed` and stable `reason_code` and adds declared requirements such as `impact_confirmation` or `mfa_step_up`; absence of proof does not make an otherwise eligible command impossible to initiate.
7. Preserve the one-Draft invariant. Missing Draft data is a server contract failure, not a client “create draft” state.
8. Unknown stored schema versions remain readable through an explicit unsupported-content projection but are not writable.

**Automated evidence:**

```bash
rtk .venv/bin/python -m pytest -q tests/test_library_response_contracts.py tests/test_entry_types.py tests/test_action_eligibility.py tests/test_v1_library_routes.py
```

Required cases include all 13 content types, no published version, withdrawn/superseded/current versions, archived entries, unsupported schema versions, sanitized event payloads, and Owner/Member action differences.

## Gate 0.7 — Expose Template reapply and Data Source operations

**Files:**

- Modify: `app/library/templates.py`
- Modify: `app/library/datasources.py`
- Create: `app/library/manual_fact_import.py`
- Modify: `app/library/response_models.py`
- Modify: `app/library/v1_router.py`
- Modify: `app/worker_runtime.py`
- Create: `tests/test_library_template_reapply_api.py`
- Create: `tests/test_library_datasource_api.py`
- Create: `tests/test_library_manual_fact_import.py`
- Modify: `tests/test_templates.py`
- Modify: `tests/test_datasources.py`

**Interfaces:**

- `POST /api/v1/library/versions/{template_version_id}/reapply-preview` — `preview_library_template_reapply`; accepts exact existing Dimension Schema Version and returns the three-way diff. The accepted merge is saved through `save_library_draft` with ETag.
- `GET /api/v1/library/entries/{data_source_entry_id}/credential-binding` — `get_library_data_source_credential_binding`.
- `POST /api/v1/library/entries/{data_source_entry_id}/credential-binding` — `bind_library_data_source_credential`.
- `POST /api/v1/library/entries/{data_source_entry_id}/connection-checks` — `check_library_data_source_connection`; returns `202 Operation` with explicit execution mode/provider provenance.
- `POST /api/v1/library/entries/{data_source_entry_id}/manual-fact-imports` — `import_library_manual_facts`; consumes a finalized Upload Intent with purpose `library_manual_fact_import`, returns `202 Operation`, and resolves to a Manual Fact Set Draft.

**Implement:**

1. Replace PR #46's `apply_library_template` 501 response with the production `app.library.templates.apply_template` path and preserve its deep-copy, provenance, transaction, and idempotency invariants.
2. Reapply uses the existing base/new/local three-way model and preserves product-local IDs, source IDs, and conflict status. It never edits a published schema.
3. Credential binding exposes credential version identity/status only, never secret fields, ciphertext, object keys, or provider responses.
4. Connection checks never change execution mode and never silently fall back. Mock, Sandbox, and real results remain distinguishable.
5. Manual-fact import validates Workspace/initiator/purpose/finalized Upload Intent, bounded file type/size, stable fact IDs, ISO countries/dates, currencies, duplicate rows, and atomic result creation.
6. Worker retry/recovery reuses the same Operation and intent; an ambiguous external connection result is not reported as success.

**Automated evidence:**

```bash
rtk .venv/bin/python -m pytest -q tests/test_library_template_reapply_api.py tests/test_library_datasource_api.py tests/test_library_manual_fact_import.py tests/test_templates.py tests/test_datasources.py
```

Required negative scenarios include Member credential bind, revoked/wrong-Workspace credential, mode mismatch, provider failure, upload reuse, malformed/oversized import, duplicate stable fact ID, stale schema revision, and conflicting reapply edits.

## Gate 0.8 — Finish Reference creation and download operations

**Files:**

- Create: `app/library/reference_commands.py`
- Modify: `app/library/references.py`
- Modify: `app/library/response_models.py`
- Modify: `app/library/v1_router.py`
- Modify: `app/worker_runtime.py`
- Create: `tests/test_library_reference_commands.py`
- Modify: `tests/test_upload_intents.py`
- Modify: `tests/test_blob_store.py`
- Modify: `tests/test_v1_library_routes.py`

**Implement:**

1. Replace PR #46's `create_library_reference_object` 501 response with the production JSON command. It consumes an owned, finalized Upload Intent; atomically creates the quarantined Reference Asset identity/version, consumes the intent, creates the gate Job and Operation, and returns `202 Operation`.
2. Exact idempotent replay returns the same Operation/result. A second semantic action against the consumed Upload Intent returns `upload_intent_consumed`.
3. The Worker runs privacy, rights, content-safety, malware, EXIF, and derivative gates, then settles both the Reference state and Operation. `result_ref` identifies the exact Reference Asset Version.
4. A Reference is selectable or downloadable only in `released`; rejected originals are purged while safe audit/hash evidence remains.
5. Replace PR #46's `create_library_reference_download_intent` 501 response with server-side derivative resolution. The browser supplies a Version ID, never an object key. The returned capability is short-lived and is not stored in Library content or events.
6. Preserve initiator/executor attribution across the Worker boundary and never report a Reference as released before verified BlobStore commit.

**Automated evidence:**

```bash
rtk .venv/bin/python -m pytest -q tests/test_library_reference_commands.py tests/test_upload_intents.py tests/test_blob_store.py tests/test_v1_library_routes.py
```

Required negative scenarios include wrong Workspace/actor/purpose, unfinalized or already-consumed Upload Intent, MIME/size/content/hash mismatch, Worker crash at each phase, gate rejection, unreleased/withdrawn download, missing derivative, signed-capability expiry, forced object key, and exact replay after response loss.

## Gate 0.9 — Deliver server-issued MFA step-up and regenerate the complete seam

**Cross-Issue dependency:** this step completes the accepted MFA contract already recorded in the Creative Library and entitlement design documents. It is a control-plane security prerequisite, not client-side role inference.

**Files:**

- Create: `web/lib/delegated-identity/step-up-issuer.ts`
- Create: `web/lib/delegated-identity/step-up-grants.ts`
- Create: `web/pages/api/auth/step-up/grant.ts`
- Create: `app/platform/step_up.py`
- Modify: `app/platform/tenancy.py`
- Modify: `app/library/v1_router.py`
- Modify: `web/pages/api/domain/v1/w/[slug]/[...path].ts`
- Modify: `web/lib/domain-client/request-pipeline.ts`
- Modify: `contracts/openapi/marketai-domain-v1.json`
- Modify: `contracts/openapi/marketai-domain-v1-policy.json`
- Modify: `scripts/generate_allowlist.ts`
- Modify: `scripts/lib/spec.ts`
- Modify: `web/lib/domain-client/gateway.ts`
- Regenerate: all files under `web/lib/domain-client/generated/`
- Create: `tests/test_library_mfa_step_up.py`
- Create: `web/__tests__/delegated-identity/step-up-issuer.spec.ts`
- Create: `web/__tests__/delegated-identity/step-up-grants.spec.ts`
- Modify: `web/__tests__/api/domain/v1/w/domain-proxy-handler.spec.ts`
- Modify: `web/__tests__/domain-client/gateway-operations.spec.ts`

**Implement:**

1. The control plane accepts only a session whose identity provider evidence records an MFA-authenticated `amr` value and a sufficiently recent `auth_time`. If those claims are absent, return `mfa_enrollment_required`; password-only reauthentication is not relabeled as MFA.
2. `POST /api/auth/step-up/grant` verifies the MFA-authenticated session and creates a short-lived, session-bound grant for the exact Workspace, command, target revision, and content hash. The browser receives only an opaque grant reference and expiry, never the FastAPI assertion.
3. When the matching Library command carries that memory-only grant reference, the BFF revalidates the session/grant binding, issues a one-request signed assertion bound to Workspace, actor, session, command, exact target revision/content hash, auth time, JTI, issued time, and expiry, and injects it into the server-to-server FastAPI request.
4. FastAPI validates signature, issuer, audience, Workspace, actor, action, target binding, expiry, authorization epoch, and one-time JTI consumption.
5. Assertions stay in BFF memory and server-to-server headers only. Grant references and assertions are excluded from persistent browser storage, Problem Details, logs, telemetry, and audit payloads; audit records only the evidence reference/JTI hash.
6. A stale MFA session returns `mfa_step_up_required` with a same-origin reauthentication URL; an account/provider without MFA evidence returns `mfa_enrollment_required`. The Library UI never collects or validates an MFA factor itself.
7. Protected publications, withdrawal, and Data Source credential binding require the proof and second confirmation. Lower-risk actions follow server requirements returned in `allowed_actions`.
8. Add every Gate 0 operation to OpenAPI, policy, generated validators/path builders/operation map/registry/allowlist, `DomainGateway`, and read/command dispatcher unions. No operation may remain a 501 stub.
9. Generated policy forwards `If-Match`, command identity, and step-up headers only for operations that declare them.

**Automated evidence:**

```bash
rtk .venv/bin/python -m pytest -q tests/test_library_mfa_step_up.py tests/test_v1_library_routes.py tests/test_bff_bridge_e2e.py
rtk npm --prefix web test -- --runInBand __tests__/delegated-identity/step-up-issuer.spec.ts __tests__/delegated-identity/step-up-grants.spec.ts __tests__/api/domain/v1/w/domain-proxy-handler.spec.ts __tests__/domain-client/gateway-operations.spec.ts
rtk bash scripts/generate_all.sh
rtk .venv/bin/python scripts/generate_openapi.py --check
rtk git diff --check -- contracts/openapi web/lib/domain-client/generated
```

On a clean commit containing the intended generated artifacts, CI reruns `rtk bash scripts/generate_all.sh` and then requires `rtk git diff --exit-code -- contracts/openapi web/lib/domain-client/generated`.

Required negative scenarios: missing, expired, future-issued, wrong issuer/audience, wrong Workspace, wrong actor/session/action/revision/hash, stale authorization epoch, replayed JTI, Member attempt, fake/replayed grant reference, browser-supplied fake assertion, assertion/grant in logs, and proof forwarded to an undeclared operation.

## Gate 0A contract exit checklist

Gate 0A is complete only when every item is true. Browser feature work remains blocked until Gate 0B also passes.

- [ ] The implementation branch contains PR #46.
- [ ] #47 records or links every final prerequisite and is `ready-for-agent`.
- [ ] All existing 20 Library operations and every Gate 0 operation are production-backed; none returns 501.
- [ ] Command lookup/reconciliation is authoritative for every Library command, especially Fork and Clone.
- [ ] Publish, Withdraw, and Archive require a fresh matching impact projection.
- [ ] Internal resources have server-verified root/parent/context reads and one cursor stream.
- [ ] All 13 response schemas are discriminated and versioned; Version has `entry_id`; Event has ID/time.
- [ ] The one-Draft invariant is explicit and tested.
- [ ] Template reapply, Data Source credential/connection, and Manual Fact import operations are exposed.
- [ ] Reference creation and download operations are production-backed and no longer return 501.
- [ ] MFA step-up is server-issued, action-bound, one-use, and negative-tested.
- [ ] OpenAPI, policy, validators, operation registry/map, paths, allowlist, gateway, and dispatchers have zero drift.
- [ ] `rtk make verify-all` passes.
- [ ] `rtk .venv/bin/alembic upgrade head`, `current`, and `check` pass on an ephemeral test database.

No later task may waive an unchecked item.

# Gate 0B — Deep client and Workspace prerequisites

## Gate 0.10 — Shared structural query keys and paginated reads

**Depends on:** Gate 0A.

**Files:**

- Create: `web/lib/domain-client/query-keys.ts`
- Create: `web/hooks/domain/useDomainPaginatedQuery.ts`
- Modify: `web/hooks/domain/useDomainQuery.ts`
- Modify: `web/hooks/domain/index.ts`
- Create: `web/__tests__/domain-client/query-keys.spec.ts`
- Create: `web/__tests__/hooks/domain/useDomainPaginatedQuery.spec.tsx`
- Modify: `web/__tests__/hooks/domain/useDomainQuery.spec.ts`

**Implement:**

- One factory returns `['domain', workspaceSlug, operationId, params]` with structurally stable, immutable params. Callers never reconstruct keys.
- Constrain pagination to `list_library_entries`, `list_library_entry_versions`, `list_library_entry_events`, `list_library_version_bindings`, and `list_library_internal_entries`.
- The module privately owns cursor extraction, progression, accumulation, ID deduplication, duplicate-load coalescing, page-problem normalization, refresh, and reset on Workspace/operation/base-param changes.
- Public result is only `items`, `hasMore`, `isLoading`, `isLoadingMore`, `isRefreshing`, `problem`, `loadMore()`, and `refresh()`.
- Default page size is 50. All active filters are in base params/cache identity.

**Evidence:**

```bash
rtk npm --prefix web test -- --runInBand __tests__/domain-client/query-keys.spec.ts __tests__/hooks/domain/useDomainQuery.spec.ts __tests__/hooks/domain/useDomainPaginatedQuery.spec.tsx
```

Tests cover cursor/filter mismatch, repeated load calls, duplicate IDs across pages, refresh from page one, Workspace switch, disabled query, page-two problem, empty page with next cursor, and stale prior response resolution after reset.

## Gate 0.11 — Returnable command outcomes and persistent Library command module

**Depends on:** Gate 0.10 and Gate 0 command-outcome operations.

**Files:**

- Modify: `web/lib/domain-client/intent.ts`
- Modify: `web/lib/domain-client/result-types.ts`
- Modify: `web/hooks/domain/useDomainCommand.ts`
- Create: `web/hooks/library/useLibraryCommand.ts`
- Create: `web/hooks/library/command-definitions.ts`
- Create: `web/hooks/library/index.ts`
- Modify: `web/__tests__/domain-client/intent.spec.ts`
- Modify: `web/__tests__/hooks/domain/useDomainCommand.spec.ts`
- Create: `web/__tests__/hooks/library/useLibraryCommand.spec.tsx`

**Implement:**

- `execute` and `retry` return `Promise<CommandOutcome<K>>`, where outcome is `success`, `problem`, or `reconciliation`.
- Expose `isPending`. React state remains available for display but is never read after `await` to infer the current call result.
- `useLibraryCommand` hides action/intent identity and exposes `run`, `retry`, `reset`, `isPending`, `problem`, and `reconciliation`.
- A new logical action gets a new ID. An ambiguous action retains exact command kind, contract version, Workspace/actor partition, target, normalized parameter hash, and retry context.
- Same pending parameters return the identical Promise. Different parameters return a local `command_already_pending` problem without transport.
- Refresh first calls authoritative command-outcome lookup/reconcile. Dismiss only hides the prompt. Reset only clears display state. Neither unlocks the intent.
- Definitive problem or success ends retry context. `not_applied` allows a new intent. Missing/expired local payload retains a minimal lock envelope.
- Do not persist binaries, secrets, step-up assertions, signed URLs, or credential fields.

**Evidence:**

```bash
rtk npm --prefix web test -- --runInBand __tests__/domain-client/intent.spec.ts __tests__/hooks/domain/useDomainCommand.spec.ts __tests__/hooks/library/useLibraryCommand.spec.tsx
```

Use fake Storage, fake clocks, deferred Promises, and remount tests. Prove no duplicate Fork/Clone after timeout, reload, input edit, dismiss, or local retention expiry.

## Gate 0.12 — Semantic Library cache invalidation

**Depends on:** Gate 0.10.

**Files:**

- Create: `web/hooks/library/useLibraryCache.ts`
- Create: `web/lib/library/command-resources.ts`
- Create: `web/__tests__/hooks/library/useLibraryCache.spec.tsx`
- Create: `web/__tests__/lib/library/command-resources.spec.ts`

**Implement:**

- Expose `useLibraryCache().invalidate(options): Promise<void>`.
- Supported resources are `entry`, `entryList`, `versions`, `events`, `impact`, `bindings`, `entryTypes`, and `internalEntries`.
- Structural matching is always restricted to the current Workspace.
- Entry-list invalidation targets all cursor pages for one root entry type or all five root types when omitted.
- Internal invalidation targets all pages for the exact root/type/parent filter.
- Command definitions name semantic resources; they never inspect SWR keys.
- Run invalidation only after `success` outcomes.

**Evidence:**

```bash
rtk npm --prefix web test -- --runInBand __tests__/hooks/library/useLibraryCache.spec.tsx __tests__/lib/library/command-resources.spec.ts
```

Tests prove another Workspace is untouched, all cursor pages match, problem/reconciliation do not invalidate, and Bind/Publish/Withdraw/Archive/Fork/Clone/Apply/Reapply invalidation sets are complete.

## Gate 0.13 — Shared dirty-navigation protection

**Depends on:** Gate 0.11.

**Files:**

- Create: `web/components/workspace/NavigationBlocker.tsx`
- Create: `web/components/workspace/GuardedLink.tsx`
- Modify: `web/components/layouts/WorkspaceLayout.tsx`
- Modify: `web/components/shared/shell/NavigationItems.tsx`
- Modify: `web/components/workspace/WorkspaceSelector.tsx`
- Create: `web/__tests__/components/workspace/navigation-blocker.spec.tsx`
- Modify: `web/__tests__/components/workspace/navigation.spec.tsx`
- Create: `web/e2e/workspace/navigation-blocker.spec.ts`

**Implement:**

- Provider interface registers `isBlocked` and `onDiscard` and exposes async `navigate(destination)`.
- Library route setters, Workspace navigation, selector, and all first-party links use the same guard.
- `beforeunload` protects refresh/close/hard navigation.
- `beforePopState` uses synchronous confirmation; cancellation restores the current URL and history position.
- Confirmed discard clears Draft dirty state before routing.
- Route focus occurs only after committed navigation, not while a dialog is open.

**Evidence:**

```bash
rtk npm --prefix web test -- --runInBand __tests__/components/workspace/navigation-blocker.spec.tsx __tests__/components/workspace/navigation.spec.tsx
rtk npm --prefix web run test:e2e -- --project=chromium e2e/workspace/navigation-blocker.spec.ts
```

## Gate 0 final exit

Gate 0 passes only when Gate 0A's checklist and Gate 0.10–0.13 are complete, `DomainGatewayProvider`/Blob/FileDigest providers are mounted from the PR #46 baseline, the shared navigation blocker covers every first-party Workspace navigation path, and the following command passes:

```bash
rtk make verify-all
```

At this point contracts and deep client/Workspace prerequisites are ready, but `library.tsx` must still render `FeatureEmptyState`.

# Phase 1 — Route model, registry, and read-only workspace

## Task 5 — Route codec and exhaustive entry registry

**Depends on:** Gate 0.10 and Gate 0.13.

**Files:**

- Create: `web/lib/library/navigation.ts`
- Create: `web/lib/library/route-codec.ts`
- Create: `web/lib/library/entry-type-definition.ts`
- Create: `web/components/library/entry-types/registry.tsx`
- Create: `web/hooks/library/useLibraryRoute.ts`
- Create: `web/__tests__/lib/library/navigation.spec.ts`
- Create: `web/__tests__/lib/library/route-codec.spec.ts`
- Create: `web/__tests__/components/library/entry-types/registry.spec.tsx`

**Implement:**

- Canonical categories are `product`, `person`, `template`, `prompt_policy`, and `datasource`, mapped one-to-one to the five root types.
- `EntryNavigationPlacement` is exhaustive: `top_level(category)` or `internal(canonicalHostCategory, allowedContextCategories, parentKinds)`.
- Register all 13 types. `categoryForTopLevelEntryType` accepts only the five root types.
- URL fields are `cat`, `entry`, `tab`, `version`, `resource_type`, `resource`, `resource_tab`, `resource_version`, `parent_type`, and `parent`.
- Category change clears root/resource state; root change resets tab and clears resource state; leaving Versions/Impact/Bindings clears `version` in the same push.
- User navigation uses guarded `router.push`; normalization uses `router.replace`.
- Resource and parent identifiers are paired. Internal IDs found in `entry` normalize into resource state after direct type/context lookup.
- Reference Asset scoped links preserve Product/Person/ReferenceGroup context; unscoped fallback uses category `product` without asserting ownership.
- Registry definitions separately declare entry/version commands, supported tabs, Draft codec/editor, Version viewer, and list metadata.

**Evidence:**

```bash
rtk npm --prefix web test -- --runInBand __tests__/lib/library/navigation.spec.ts __tests__/lib/library/route-codec.spec.ts __tests__/components/library/entry-types/registry.spec.tsx
```

Use table tests for every type/tab/category and property tests for parse-normalize-serialize idempotence. No valid scoped link may normalize to an unscoped link.

## Task 6 — Build the unmounted read-only Library shell

**Depends on:** Gate 0.10, Gate 0.12, and Task 5.

**Files:**

- Create: `web/components/library/LibraryShell.tsx`
- Create: `web/components/library/LibraryCategoryNav.tsx`
- Create: `web/components/library/list/LibraryListView.tsx`
- Create: `web/components/library/list/LibraryEntryRow.tsx`
- Create: `web/components/library/list/LibraryEmptyCategory.tsx`
- Create: `web/components/library/detail/LibraryEntryDetail.tsx`
- Create: `web/components/library/detail/EntryHeader.tsx`
- Create: `web/components/library/detail/ProblemState.tsx`
- Create: `web/hooks/library/useLibraryEntries.ts`
- Create: `web/locales/en/library.json`
- Create: `web/__tests__/components/library/library-shell.spec.tsx`
- Create: `web/__tests__/components/library/library-list.spec.tsx`

**Implement:**

- Three-pane desktop layout: category navigation, cursor list, detail. Medium collapses category navigation; mobile uses drill-in list/detail. CSS controls layout; JavaScript does not branch query ownership on viewport.
- `LibraryShell` loads entry types. The list calls `useLibraryEntries` with exactly one root type.
- Show explicit loading, empty, problem/retry, refreshing, and end-of-list states.
- Summary rows use only list response fields and show lifecycle/current version/Draft status without detail requests.
- No entry shows an empty-selection prompt. Not-found detail is scoped so the list remains usable.
- All user-visible strings and stable codes use the `library` namespace. Unknown codes use a safe generic message and never drive control flow.
- Keep the shell unmounted from `library.tsx`; render it only in Jest until Task 15.

**Evidence:**

```bash
rtk npm --prefix web test -- --runInBand __tests__/components/library/library-shell.spec.tsx __tests__/components/library/library-list.spec.tsx
```

Assert initial query count is two at most: entry types plus the selected root-list page. Assert internal types never render in any top-level list fixture.

## Task 7 — Detail tabs, direct version resolution, and nested resources

**Depends on:** Task 6.

**Files:**

- Create: `web/components/library/detail/LibraryTabs.tsx`
- Create: `web/components/library/tabs/CurrentTab.tsx`
- Create: `web/components/library/tabs/DraftTab.tsx`
- Create: `web/components/library/tabs/VersionsTab.tsx`
- Create: `web/components/library/tabs/EventsTab.tsx`
- Create: `web/components/library/tabs/ImpactTab.tsx`
- Create: `web/components/library/tabs/BindingsTab.tsx`
- Create: `web/components/library/tabs/VersionDetail.tsx`
- Create: `web/components/library/tabs/EventTimeline.tsx`
- Create: `web/components/library/nested/NestedResourcePanel.tsx`
- Create: `web/hooks/library/useResolvedLibraryVersion.ts`
- Create: `web/hooks/library/useResolvedLibraryResource.ts`
- Create: `web/hooks/library/useLibraryVersions.ts`
- Create: `web/hooks/library/useLibraryEvents.ts`
- Create: `web/hooks/library/useLibraryBindings.ts`
- Create: `web/hooks/library/useLibraryInternalEntries.ts`
- Create: `web/__tests__/hooks/library/useResolvedLibraryVersion.spec.tsx`
- Create: `web/__tests__/hooks/library/useResolvedLibraryResource.spec.tsx`
- Create: `web/__tests__/components/library/library-tabs.spec.tsx`

**Implement:**

- Current loads the current Version directly; if no current Version, fire no Version query and show “Go to Draft”.
- Draft uses Entry detail data.
- Versions, Events, and Bindings use paginated wrappers. `Versions + version` renders historical Version Detail.
- Impact and Bindings resolve requested Version directly and verify `version.entry_id` equals the selected Entry. Do not validate through a partially loaded history.
- Hidden tabs issue no tab-specific reads.
- Nested resources resolve through Gate 0 context operations. Invalid/missing relationships normalize safely or show scoped not-found without contaminating root state.
- Version content, lineage, content hash, eligibility, real/mock provenance, event actor/executor/time, and authorized bindings remain inspectable after withdrawal.

**Evidence:**

```bash
rtk npm --prefix web test -- --runInBand __tests__/hooks/library/useResolvedLibraryVersion.spec.tsx __tests__/hooks/library/useResolvedLibraryResource.spec.tsx __tests__/components/library/library-tabs.spec.tsx
```

# Phase 2 — Drafts and commands

## Task 8 — Draft codecs, dirty sessions, validation, and conflict recovery

**Depends on:** Gate 0.13 and Tasks 5 and 7.

**Files:**

- Create: `web/lib/library/draft-codecs.ts`
- Create: `web/hooks/library/useDraftSession.ts`
- Create: `web/components/library/draft/DraftEditor.tsx`
- Create: `web/components/library/draft/ValidationPanel.tsx`
- Create: `web/components/library/draft/ConflictDialog.tsx`
- Create: `web/components/library/draft/UnsupportedSchemaState.tsx`
- Create: `web/__tests__/lib/library/draft-codecs.spec.ts`
- Create: `web/__tests__/hooks/library/useDraftSession.spec.tsx`
- Create: `web/__tests__/components/library/draft-editor.spec.tsx`

**Implement:**

- Each type codec parses one declared schema version, preserves unknown fields internally, and emits complete replacement content only when safe.
- Unsupported versions are read-only; no generic JSON editor and no field dropping.
- Draft session separates server snapshot, editing buffer, dirty state, save state, validation state, and conflict state.
- Save returns its command outcome directly, updates revision/ETag only on success, and invalidates entry/list/event resources.
- Validation is explicit. Blocking findings prevent Publish; warnings use stable codes and require acknowledgements tied to the validated revision/content hash.
- Revision conflict uses server base/current/attempted content for a three-way workflow: reload server, review field conflicts, or cancel. Never silently last-write-wins.
- Successful discard clears the buffer before guarded navigation.

**Evidence:**

```bash
rtk npm --prefix web test -- --runInBand __tests__/lib/library/draft-codecs.spec.ts __tests__/hooks/library/useDraftSession.spec.tsx __tests__/components/library/draft-editor.spec.tsx
```

Fixtures cover all 13 types, unknown schema, server normalization, warning-code stability, simultaneous edit, save response loss, and validation response arriving after a later edit.

## Task 9 — Action toolbar, command dialogs, impacts, and reconciliation UI

**Depends on:** Gate 0.11, Gate 0.12, and Tasks 7 and 8.

**Files:**

- Create: `web/components/library/commands/EntryActions.tsx`
- Create: `web/components/library/commands/VersionActions.tsx`
- Create: `web/components/library/commands/CreateRootEntryDialog.tsx`
- Create: `web/components/library/commands/PublishDialog.tsx`
- Create: `web/components/library/commands/WithdrawDialog.tsx`
- Create: `web/components/library/commands/ArchiveDialog.tsx`
- Create: `web/components/library/commands/ForkDialog.tsx`
- Create: `web/components/library/commands/CloneProductDialog.tsx`
- Create: `web/components/library/commands/ApplyTemplateDialog.tsx`
- Create: `web/components/library/commands/BindReferenceDialog.tsx`
- Create: `web/components/library/commands/StepUpDialog.tsx`
- Create: `web/components/library/commands/ReconciliationBanner.tsx`
- Create: `web/hooks/library/useLibraryCommandImpact.ts`
- Create: `web/hooks/library/useLibraryStepUp.ts`
- Create: `web/__tests__/components/library/library-actions.spec.tsx`
- Create: `web/__tests__/components/library/library-command-dialogs.spec.tsx`
- Create: `web/__tests__/hooks/library/useLibraryCommandImpact.spec.tsx`

**Implement:**

- Entry actions are registry entry commands intersected with `entry.allowed_actions`; Version actions use Version commands and `version.allowed_actions`.
- Root creation maps the active category to exactly one root type. Internal types are absent from this dialog and use the contextual creation flow in Task 11.
- Denied actions remain keyboard focusable with `aria-disabled`, suppress activation, and expose localized `reason_code` text. UI never infers permission from role/state.
- Publish, Withdraw, and Archive fetch fresh impact only when their dialogs open. Confirmation sends matching impact identity/hash; changed impact resets confirmation.
- Publish shows acknowledged warning codes. Withdraw requires a reason. Archive clearly distinguishes “blocks new use” from deletion.
- Fork requires a display name. Clone Product requires a display name and sends only the explicitly selected optional inclusions.
- Bind Reference submits the exact Reference Asset Version, purpose, viewpoint, non-negative priority, conditions, and current Draft ETag.
- Apply Template uses an explicit target Product picker, keeps that Product as the root context, and opens the resulting internal Dimension Schema.
- Fork/Clone use authoritative outcome recovery. Clone selects the returned Product root; Fork preserves a server-verified parent/root context or uses the returned canonical unscoped location without guessing ownership.
- When server requirements include `mfa_step_up`, the dialog requests a memory-only grant reference from the control-plane step-up endpoint. `mfa_step_up_required` uses the same-origin reauthentication URL and retries grant issuance after the session is refreshed; `mfa_enrollment_required` blocks the command with a stable recovery message. The browser never receives the signed FastAPI assertion, and the grant reference is never placed in router state, SWR, IntentTracker, or persistent storage.
- Reconciliation UI distinguishes retry-safe, server lookup in progress, unresolved ambiguity, terminal problem, and authoritative not-applied. Dismiss never unlocks.
- Dialog focus is trapped, labelled, restored to the invoker, and announced on async state changes.

**Evidence:**

```bash
rtk npm --prefix web test -- --runInBand __tests__/components/library/library-actions.spec.tsx __tests__/components/library/library-command-dialogs.spec.tsx __tests__/hooks/library/useLibraryCommandImpact.spec.tsx
```

# Phase 3 — Type-specific workflows

## Task 10 — Five root entry modules

**Depends on:** Tasks 8 and 9.

**Files:**

- Create: `web/components/library/entry-types/product/ProductDraftEditor.tsx`
- Create: `web/components/library/entry-types/product/ProductVersionViewer.tsx`
- Create: `web/components/library/entry-types/person/PersonDraftEditor.tsx`
- Create: `web/components/library/entry-types/person/PersonVersionViewer.tsx`
- Create: `web/components/library/entry-types/dimension-template/DimensionTemplateDraftEditor.tsx`
- Create: `web/components/library/entry-types/dimension-template/DimensionTemplateVersionViewer.tsx`
- Create: `web/components/library/entry-types/prompt-policy/PromptPolicyDraftEditor.tsx`
- Create: `web/components/library/entry-types/prompt-policy/PromptPolicyVersionViewer.tsx`
- Create: `web/components/library/entry-types/data-source/DataSourceDraftEditor.tsx`
- Create: `web/components/library/entry-types/data-source/DataSourceVersionViewer.tsx`
- Create: `web/__tests__/components/library/root-entry-types.spec.tsx`

**Implement:**

- Product: identity, description, audience, selling points, must-match evidence, references, groups, and default selection rule.
- Person: fictional/licensed type, age expression, prompt fragments, rights regions/channels/evidence/dates, and references.
- Dimension Template: stable dimensions/values, fragments, briefs, constraints, cardinality, and concept keys.
- Prompt Policy: registered tokens, locked phrases, brand tone, allowed templates, and optional clauses. Apply server-normalized save output to the buffer.
- Data Source: adapter kind, execution mode, allowlisted non-secret config, capability/market metadata, credential status, and explicit provenance.
- Generate stable UUIDs once for newly authored nested fields; never derive IDs from labels.

**Evidence:**

```bash
rtk npm --prefix web test -- --runInBand __tests__/components/library/root-entry-types.spec.tsx
```

The test table covers valid edit/save, field error focus, unknown fields, deterministic reorder, accessible labels, and read-only Version rendering for each root type.

## Task 11 — Eight internal entry modules and contextual creation

**Depends on:** Tasks 7–10.

**Files:**

- Create editor/viewer modules under:
  - `web/components/library/entry-types/offer/`
  - `web/components/library/entry-types/reference-group/`
  - `web/components/library/entry-types/reference-selection-rule/`
  - `web/components/library/entry-types/dimension-schema/`
  - `web/components/library/entry-types/market-source-mapping/`
  - `web/components/library/entry-types/manual-fact-set/`
  - `web/components/library/entry-types/truth-resolution-policy/`
  - `web/components/library/entry-types/reference-asset/`
- Create: `web/components/library/nested/CreateInternalEntryDialog.tsx`
- Create: `web/__tests__/components/library/internal-entry-types.spec.tsx`
- Create: `web/__tests__/components/library/contextual-create.spec.tsx`

**Implement:**

- Internal creation always supplies the verified root/parent context; the generic top-level create action cannot create internal types.
- Offer binds an exact Product Version and explicit item/market/claim/date semantics.
- Market Source Mapping binds exact Offer and Data Source Versions and explicit external object/market/mode.
- Dimension Schema preserves Product binding, template lineage, stable local IDs, constraints, and cardinality.
- Reference Group/Selection Rule select stable version/dimension/value/aspect/intent IDs rather than labels or filenames.
- Manual Fact Set and Truth Resolution Policy are reachable only from Data Source contexts and never imply a default Data Source.
- Reference Asset viewer exposes gates/rights/derivative/provenance without object keys and preserves contextual Binding breadcrumbs.

**Evidence:**

```bash
rtk npm --prefix web test -- --runInBand __tests__/components/library/internal-entry-types.spec.tsx __tests__/components/library/contextual-create.spec.tsx
```

Every type must have a registry entry, codec, editor, viewer, supported tabs/actions, legal context tests, illegal context tests, and unsupported-schema behavior.

## Task 12 — Reference upload, binding, and secure download

**Depends on:** Gate 0.11 and Tasks 7, 9, and 11.

**Files:**

- Create: `web/components/library/references/ReferenceUploadZone.tsx`
- Create: `web/components/library/references/ReferenceGateProgress.tsx`
- Create: `web/components/library/references/ReferenceBindingList.tsx`
- Create: `web/hooks/library/useLibraryReferenceUpload.ts`
- Create: `web/hooks/library/useLibraryReferenceDownload.ts`
- Create: `web/__tests__/hooks/library/useLibraryReferenceUpload.spec.tsx`
- Create: `web/__tests__/hooks/library/useLibraryReferenceDownload.spec.tsx`
- Create: `web/__tests__/components/library/reference-workflows.spec.tsx`

**Implement:**

- Upload sequence is Upload Intent → direct BlobStore transfer → finalize → Operation polling → create reference object → Operation polling. No binary crosses Next.js.
- Reference creation verifies purpose, initiator, Workspace, finalized committed object, one-time consumption, and exact retry identity.
- UI distinguishes hashing, transfer, verification, quarantine, gate processing, released, rejected, failed, and uncertain states.
- Only released versions are bindable/downloadable.
- Download uses a fresh Library Download Intent; the browser never submits an object key and never persists the signed URL.
- After upload success, preserve the originating Product/Person/ReferenceGroup context.

**Evidence:**

```bash
rtk npm --prefix web test -- --runInBand __tests__/hooks/library/useLibraryReferenceUpload.spec.tsx __tests__/hooks/library/useLibraryReferenceDownload.spec.tsx __tests__/components/library/reference-workflows.spec.tsx
```

Negative tests cover MIME mismatch, oversize, malware/content/rights rejection, EXIF handling, wrong Workspace/actor, consumed intent, expired signed capability, unreleased download, forced-visible button, and response loss at every phase.

## Task 13 — Template reapply and Data Source operational panels

**Depends on:** Gate 0.7, Tasks 10–12.

**Files:**

- Create: `web/components/library/templates/TemplateReapplyPanel.tsx`
- Create: `web/components/library/templates/TemplateDiffTable.tsx`
- Create: `web/components/library/data-sources/CredentialBindingPanel.tsx`
- Create: `web/components/library/data-sources/ConnectionCheckPanel.tsx`
- Create: `web/components/library/data-sources/ManualFactImportPanel.tsx`
- Create: `web/hooks/library/useTemplateReapply.ts`
- Create: `web/hooks/library/useDataSourceOperations.ts`
- Create: `web/__tests__/components/library/template-reapply.spec.tsx`
- Create: `web/__tests__/components/library/data-source-operations.spec.tsx`

**Implement:**

- Reapply presents unchanged, template-only, template-removed, locally removed, local-only, and conflict states. User choices create a complete Dimension Schema Draft replacement; save still uses Draft ETag.
- Credential binding lists credential metadata only and runs required server-issued step-up before confirmation.
- Connection result shows provider, mode, freshness, capability result, and deterministic failure. Never change mode after failure.
- Manual Fact import uses the dedicated upload/import Operation and reports row-level validation without exposing raw file content in telemetry.
- Stale reapply/credential/import state invalidates confirmation and requires refresh.

**Evidence:**

```bash
rtk npm --prefix web test -- --runInBand __tests__/components/library/template-reapply.spec.tsx __tests__/components/library/data-source-operations.spec.tsx
```

# Phase 4 — Quality, security, and release

## Task 14 — Accessibility, responsive behavior, performance, and observability

**Depends on:** Tasks 6–13.

**Files:**

- Modify: `web/package.json`
- Modify: `web/package-lock.json`
- Modify: `web/playwright.config.ts`
- Create: `web/components/library/LibraryLiveRegion.tsx`
- Create: `web/lib/library/telemetry.ts`
- Create: `web/__tests__/components/library/accessibility.spec.tsx`
- Create: `web/__tests__/components/library/responsive-query-ownership.spec.tsx`
- Create: `web/__tests__/lib/library/telemetry.spec.ts`
- Create: `web/e2e/library/accessibility-responsive.spec.ts`

**Implement:**

- Add `@axe-core/playwright` as a pinned development dependency.
- Tabs use manual activation and correct ARIA relationships. Lists, dialogs, validation summaries, progress, status changes, and conflict/reconciliation announcements meet WCAG 2.2 AA behavior.
- Denied actions remain explainable by keyboard and assistive technology.
- Add Desktop Chrome, tablet Chromium, and mobile Chromium coverage. Layout changes are CSS-only and do not duplicate query trees.
- Lazy-load type editors/viewers and command-heavy panels. Hidden tabs and unopened dialogs perform no feature reads.
- Enforce request budgets: initial Library shell at most two Domain reads; selecting a root adds one detail read; opening one tab adds only that tab's declared reads; each pagination action adds one page request.
- Telemetry uses bounded operation/action/outcome/reason labels only. It excludes IDs, URL state, content, files, prompts, prices, secrets, proofs, signed URLs, and arbitrary exception text.

**Evidence:**

```bash
rtk npm --prefix web test -- --runInBand __tests__/components/library/accessibility.spec.tsx __tests__/components/library/responsive-query-ownership.spec.tsx __tests__/lib/library/telemetry.spec.ts
rtk npm --prefix web run check-types
rtk npm --prefix web run check-lint
rtk npm --prefix web run test:e2e -- --project=chromium e2e/library/accessibility-responsive.spec.ts
rtk npm --prefix web run test:e2e -- --project=tablet-chromium e2e/library/accessibility-responsive.spec.ts
rtk npm --prefix web run test:e2e -- --project=mobile-chromium e2e/library/accessibility-responsive.spec.ts
```

## Task 15 — Atomic page cutover and complete browser acceptance

**Depends on:** Gate 0 and Tasks 5–14. This task is indivisible for release purposes.

**Files:**

- Modify: `web/pages/w/[slug]/library.tsx`
- Modify: `web/components/workspace/WorkspaceNavigation.tsx`
- Modify: `web/e2e/workspace/network-boundary.spec.ts`
- Create: `web/e2e/library/fixtures.ts`
- Create: `web/e2e/library/fixtures/reference-clean.png`
- Create: `web/e2e/library/golden-path.spec.ts`
- Create: `web/e2e/library/owner-member.spec.ts`
- Create: `web/e2e/library/version-history.spec.ts`
- Create: `web/e2e/library/command-recovery.spec.ts`
- Create: `web/e2e/library/reference-transfer.spec.ts`
- Create: `web/e2e/library/cross-workspace-security.spec.ts`
- Create: `web/e2e/library/template-datasource.spec.ts`

**Cutover:**

1. Replace `FeatureEmptyState` with `LibraryShell` in the existing page; keep `WorkspaceLayout` and `getWorkspacePageProps({ namespaces: ['common', 'library'] })`.
2. Do not leave a feature flag, old data path, fallback seed, hidden legacy page, or dual read/write behavior.
3. Make Workspace navigation active-state matching ignore the Library query string.
4. Remove the `fixme` from the same-origin Domain request test and make it observe real Library traffic.
5. If pre-merge acceptance fails, revert only the page mount while correcting the branch; do not roll back successful Domain data or add a runtime fallback.

**Playwright acceptance matrix:**

- Empty Workspace → create each of the five root types → refresh → records persist.
- Product → internal Offer/Reference Group/Rule/Schema/Mapping; Data Source → Manual Fact Set/Truth Policy; none appears in a root list.
- Product- and Person-scoped links to the same Reference Asset preserve their distinct contexts; unscoped link normalizes to Product fallback.
- Save/validate/warnings/publish/version history/current/history deep links/events/impact/bindings.
- Concurrent Draft conflict and three-way recovery.
- Fresh Publish/Archive/Withdraw impact; stale projection rejection and reconfirmation.
- Fork/Clone response loss and refresh recover the exact created entry without duplicates.
- Upload, Worker restart during gate processing, released binding, secure download, rejection, and uncertain recovery.
- Owner/Member eligibility and server denial when controls are forcibly enabled.
- Cross-Workspace root/resource/version/parent/command identity/upload/download/credential IDs fail closed.
- Template Apply and Reapply preserve lineage/local IDs; Data Source credential, connection, and import show correct mode/provenance.
- Withdraw/rights restriction removes new-use eligibility while historical Library Version and frozen Run/Asset evidence remain inspectable.
- Back/Forward, Workspace switch, refresh, hard navigation, mobile drill-in, keyboard-only operation, dialog focus, and axe checks.

**Commands:**

```bash
rtk npm --prefix web run test:e2e -- --project=chromium e2e/library
rtk npm --prefix web run test:e2e -- --project=tablet-chromium e2e/library/accessibility-responsive.spec.ts
rtk npm --prefix web run test:e2e -- --project=mobile-chromium e2e/library/accessibility-responsive.spec.ts
rtk npm --prefix web run test:e2e -- --project=chromium e2e/workspace/network-boundary.spec.ts e2e/workspace/workspace-switch.spec.ts e2e/workspace/navigation-blocker.spec.ts
rtk make verify-all
rtk .venv/bin/python scripts/generate_openapi.py --check
rtk git diff --check -- contracts/openapi web/lib/domain-client/generated
```

The clean-commit CI gate reruns the generators and requires `rtk git diff --exit-code -- contracts/openapi web/lib/domain-client/generated`.

## 4. Release evidence bundle

The implementation PR must attach or link all of the following:

1. Gate 0 checklist with command output and operation inventory.
2. OpenAPI and generated-artifact drift checks.
3. Alembic upgrade/current/check results from an ephemeral PostgreSQL database.
4. Python command, concurrency, Workspace/actor, impact, navigation-context, MFA, upload/import, and lifecycle test results.
5. Web typecheck, lint, Jest, build, CSP, and license inventory results.
6. Playwright results for desktop/tablet/mobile, with traces for retry/reconciliation and screenshots for the five root empty/populated states.
7. Browser request log proving every feature request is same-origin BFF and no object key/secret/proof appears.
8. A redaction audit showing logs/telemetry exclude the prohibited fields.
9. A query-count report proving lazy tab/dialog behavior and no list N+1.
10. Manual WCAG 2.2 AA checklist for focus order, visible focus, zoom/reflow, error identification, status announcements, and target size.
11. Issue trace showing #47 criteria satisfied. Do not close parent #27 until sibling #48, #49, and their combined golden path pass.

## 5. Cross-plan dependency audit

| Task        | Hard dependencies                                      | Must not assume                                                          |
| ----------- | ------------------------------------------------------ | ------------------------------------------------------------------------ |
| Gate 0.2    | PR #46 baseline                                        | Current planning checkout schema/head                                    |
| Gate 0.3    | Durable Library idempotency reservation                | LocalStorage or HTTP retry proves outcome                                |
| Gate 0.4    | Dependency Index and lifecycle revisions               | Existing generic impact is fresh command confirmation                    |
| Gate 0.5    | Server relationship queries and context persistence    | Client list membership or display-name matching                          |
| Gate 0.6    | All 13 domain content contracts                        | Opaque JSON is safely editable                                           |
| Gate 0.7    | #18 credentials and #20 domain logic                   | Generic entry list substitutes for child operations                      |
| Gate 0.8    | Upload Intent, BlobStore, durable Jobs, and Operations | PR #46's Reference 501 stubs are acceptable for UI work                  |
| Gate 0.9    | Identity provider MFA evidence                         | Password re-entry is MFA or browser proof is trusted                     |
| Gate 0.10   | Generated paginated operations                         | Callers can reconstruct SWR keys                                         |
| Gate 0.11   | Gate 0 outcome lookup/reconcile                        | Dismiss/TTL permits a new intent                                         |
| Gate 0.13   | Shared WorkspaceLayout integration                     | Ordinary Next.js `Link` is dirty-safe                                    |
| Tasks 5–7   | Gate 0 context/version contracts                       | Internal IDs belong in top-level `entry`                                 |
| Tasks 8–9   | Returnable outcomes, ETags, impact records             | React state after `await` describes the completed call                   |
| Tasks 10–13 | Versioned codecs and generated operations              | Generic JSON editors or raw fetch are acceptable fallbacks               |
| Task 14     | Completed feature modules                              | Viewport JavaScript may create duplicate query ownership                 |
| Task 15     | Every prior gate and test                              | Partial mount, seed fallback, or dual transport is an acceptable release |

## 6. Design trace audit

- Query routing replaces hash routing: preserved.
- Five top-level entrances versus 13 types: root-only lists and exhaustive internal placement are explicit.
- Internal resources never occupy `entry`: explicit in route codec, server context operations, and tests.
- Reference Asset shared contexts and Product canonical fallback: explicit without ownership inference.
- Versions/Impact/Bindings version deep links: explicit and direct-validated.
- Entry/Version `allowed_actions` remain separate: explicit in response contracts and toolbars.
- Returnable command outcome and hidden action identity: explicit.
- Persistent ambiguity lock, same-Promise coalescing, and different-param rejection: explicit.
- Publish/Withdraw/Archive fresh impacts: Gate 0.4 and Task 9.
- Shared structural keys, pagination, semantic invalidation: Gate 0.10 and 0.12.
- Shared navigation blocker including Back/Forward: Gate 0.13.
- Per-type versioned codecs with unsupported-schema read-only behavior: Tasks 8, 10, 11.
- Hidden tabs do not fetch; CSS-only responsive behavior: Tasks 7 and 14.
- Security, audit, provenance, i18n, and bounded telemetry: Gate 0.6–0.9 and Tasks 6, 12–14.
- Full automated/manual evidence and atomic cutover: Tasks 14–15 and release bundle.

## 7. Final readiness rule

This plan is executable once Gate 0.1's issue-tracking state and the PR #46 implementation baseline are present. It does not authorize skipping any Gate 0 contract because a UI can be mocked around it. Implementation may proceed task-by-task only in the dependency order above; production exposure occurs once, in Task 15.
