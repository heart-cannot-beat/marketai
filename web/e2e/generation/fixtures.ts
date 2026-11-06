import {
  test as base,
  test as setup,
  expect,
  type Page,
  type Route,
} from '@playwright/test';

/**
 * Fixtures for Slice 1 (draft + preview) E2E coverage
 * (`.superpowers/sdd/2026-09-03-generation-workspace-ui/task-7-brief.md`).
 *
 * The page shells under test (`pages/w/[slug]/generate/*`) are gated by
 * `getWorkspacePageProps()` (`lib/workspace-bootstrap.ts`), which resolves
 * the Workspace and the caller's session/membership server-side against
 * Prisma/Postgres before the page ever renders — that SSR call cannot be
 * intercepted from the browser context Playwright drives, so these specs
 * still depend on the same `chromium` project auth this repo's other E2E
 * suites use (`storageState: 'e2e/.auth/owner.json'`, produced by
 * `e2e/workspace/fixtures.ts`'s `setup` project — see `playwright.config.ts`)
 * and a seeded `workspace-a` the storage-state owner belongs to.
 *
 * Everything *below* that SSR boundary is mocked here: every
 * `get_generation_capabilities` / `list_generation_drafts` /
 * `get_generation_draft` / `create_generation_draft` /
 * `create_generation_preview` / `get_generation_preview` call the Generation
 * domain hooks make (`hooks/domain/useDomainQuery.ts`,
 * `hooks/domain/useDomainCommand.ts`) is a same-origin browser `fetch` to
 * `/api/domain/v1/w/:slug/generation/...` (`lib/domain-client/gateway.ts`,
 * `generated/paths.ts`) — a request Playwright's `page.route()` intercepts
 * before it reaches the Next.js server, so these specs need no live domain
 * API or seeded ProductVersion. The one other network dependency,
 * `GET /api/csrf` (`lib/domain-client/csrf.ts`, required before any
 * mutating call), is stubbed the same way.
 *
 * `export { setup }` mirrors `e2e/workspace/fixtures.ts`'s shape for the
 * `setup` Playwright project — this file defines no additional `setup(...)`
 * cases of its own (Slice 1 needs no generation-specific credential beyond
 * the standard owner login), but keeping the export makes this fixtures
 * file a legal (if inert) `setup`-project match if ever added to
 * `testDir`/`testMatch`.
 */
export { setup };

export const WORKSPACE_SLUG = 'workspace-a';
export const GENERATE_URL = `/w/${WORKSPACE_SLUG}/generate`;

export const DRAFT_ID = 'draft-1';

/** The one product the mocked drafts and runs belong to (`list_products`). */
export const PRODUCT_NAME = 'E2E 测试产品';
export const PRODUCT_SUMMARY = {
  product_id: 'product-1',
  entry_id: 'entry-1',
  name: PRODUCT_NAME,
  variant_label: null,
  ready: true,
  primary_reference_version_id: null,
  latest_run: null,
};
export const PREVIEW_ID = 'preview-1';
export const NONEXISTENT_DRAFT_ID = 'nonexistent-foreign-id';

/** `GenerationCapabilities` (`app/generation/response_models.py`) as
 * consumed by `NewDraftDialog`: `providers: string[]`, `models: dict[provider
 * -> dict[model_id -> {display_name, format_limits}]]`, `modes: string[]`. */
export const CAPABILITIES = {
  providers: ['openai'],
  models: {
    openai: {
      'gpt-image-1': {
        display_name: 'GPT Image 1',
        format_limits: { max_variant_count: 4 },
      },
    },
  },
  modes: ['mock', 'live'],
};

/** Raw `list_generation_drafts` item shape (`app/generation/run_queries.py::
 * _draft_summary`) — no `title`/`allowed_actions`; `GenerationListView` maps
 * this into `DraftSummary`. */
export const DRAFT_SUMMARY = {
  id: DRAFT_ID,
  product_id: 'product-1',
  provider: 'openai',
  model: 'gpt-image-1',
  execution_mode: 'mock',
  state: 'draft',
  revision: 1,
  updated_at: '2026-09-01T12:00:00Z',
};

/** `GenerationDraftDetail` shape `DraftWorkspace`/`DraftComposer` read
 * (`DraftComposerDraft` in `components/generation/draft/DraftComposer.tsx`),
 * with `create_preview` allowed so the "Generate Preview" action renders. */
export const DRAFT_DETAIL = {
  id: DRAFT_ID,
  product_id: 'product-1',
  provider: 'openai',
  model: 'gpt-image-1',
  execution_mode: 'mock',
  state: 'draft',
  revision: 1,
  selectors: {},
  combination_strategy: { strategy: 'full' },
  format_config: {},
  exclusions: {},
  allowed_actions: [
    { action: 'update', allowed: true, reason_code: null },
    { action: 'preview', allowed: true, reason_code: null },
  ],
};

/** `GenerationPreviewDetail` (discriminated on `purpose:
 * "initial_generation"`, spec: `app/generation/response_models.py`) as
 * `PreviewPanel` reads it. Also carries the `preview_hash`/`allowed_actions`
 * fields `SubmitConfirmation` (Task 9, Slice 2) needs -- `PreviewPanel`
 * itself ignores both, so extending this one shared fixture rather than
 * forking a second preview payload keeps Slice 1's `draft-preview.spec.ts`
 * (which never asserts on either field) and Slice 2's
 * `submission-progress.spec.ts` reading the exact same mocked response. */
export const PREVIEW_DETAIL = {
  id: PREVIEW_ID,
  purpose: 'initial_generation',
  draft_id: DRAFT_ID,
  execution_mode: 'mock',
  valid_until: '2099-01-01T00:00:00Z',
  invalidation_reason: null,
  combination_count: 4,
  call_count: 4,
  exclusion_summary: { excluded_count: 0, reasons: [] },
  maximum_cost: 1.23,
  final_prompt: 'A studio product photo of product-1, mock execution mode.',
  capability_decisions: [
    { capability: 'style_transfer', supported: true, detail: null },
  ],
  preview_hash: 'preview-hash-1',
  allowed_actions: [{ action: 'submit', allowed: true, reason_code: null }],
};

/**
 * Slice 2 (submission + run progress) fixtures
 * (`.superpowers/sdd/2026-09-03-generation-workspace-ui/task-10-brief.md`).
 * Mirrors the Slice 1 fixtures above one level down the flow: submitting
 * `PREVIEW_ID` (`submit_generation_run`, `SubmitConfirmation`) hands back
 * `RUN_ID`'s `OperationRef`, and `RunProgressView`
 * (`components/generation/progress/RunProgressView.tsx`, via Task 8's
 * `useRunProgress`/`PollingRunProgressSource`) polls `RUN_DETAIL`, its
 * latest command's `OPERATION_DETAIL`, and `RUN_ITEM_DETAIL` together.
 */
export const RUN_ID = 'run-1';
export const OPERATION_ID = 'operation-run-1';
export const RUN_ITEM_ID = 'run-item-1';

/** `GenerationRunItemDetail` (`get_generation_run_item` /
 * `list_generation_run_items`, `lib/domain-client/generated/domain-v1.d.ts`)
 * -- a single, already-terminal item so `RunItemGrid`/`RunItemDetail` render
 * a stable, non-polling-dependent state. */
export const RUN_ITEM_DETAIL = {
  id: RUN_ITEM_ID,
  ordinal: 1,
  state: 'succeeded',
  combination_fingerprint: 'fingerprint-1',
  variant_index: 0,
  execution_mode: 'mock',
  error_summary: '',
  intent_kind: 'new_generation',
};

/** `GenerationRunDetail` (`get_generation_run`) -- already terminal
 * (`status: 'succeeded'`) so `PollingRunProgressSource` settles after its
 * first poll instead of scheduling further timers a Playwright test would
 * otherwise have to wait out or explicitly stop. */
export const RUN_DETAIL = {
  id: RUN_ID,
  product_id: 'product-1',
  provider: 'openai',
  model: 'gpt-image-1',
  status: 'succeeded',
  total: 4,
  done: 4,
  failed: 0,
  execution_mode: 'mock',
  revision: 1,
  commands: [
    {
      id: 'command-1',
      command_kind: 'submit',
      operation_id: OPERATION_ID,
      created_at: '2026-09-01T12:00:00Z',
    },
  ],
};

/** `OperationDetailResponse` (`get_operation`, spec §5.6) behind
 * `RUN_DETAIL.commands[0]` -- also terminal, matching `RUN_DETAIL.status`. */
export const OPERATION_DETAIL = {
  operation_id: OPERATION_ID,
  command_type: 'submit_generation_run',
  state: 'succeeded',
  result: { resource_type: 'generation_run', resource_id: RUN_ID },
  created_at: '2026-09-01T12:00:00Z',
  updated_at: '2026-09-01T12:00:05Z',
};

/** Raw `list_generation_runs` item shape (`app/generation/run_queries.py::
 * _run_summary`) -- `RunList` (`components/generation/list/RunList.tsx`)
 * reads this directly, no mapping needed (unlike drafts). */
export const RUN_SUMMARY = {
  id: RUN_ID,
  product_id: 'product-1',
  status: 'succeeded',
  done: 4,
  total: 4,
  created_at: '2026-09-01T12:00:00Z',
};

/**
 * Slice 3 (recovery) fixtures
 * (`.superpowers/sdd/2026-09-03-generation-workspace-ui/task-12-brief.md`).
 * `RecoveryPanel` (Task 11, `components/generation/recovery/
 * RecoveryPanel.tsx`) only renders its action buttons once
 * `RunProgressView`'s poller reports `isRunTerminal`
 * (`RUN_TERMINAL_STATES` in `RunProgressSource.ts`) *and* the
 * `GenerationRunDetail` it reads carries `allowed_actions` -- so, unlike
 * Slice 2's single already-terminal `RUN_DETAIL` (which has no
 * `allowed_actions` at all, by design: it exists only to let
 * `RunProgressView` settle without exercising recovery), these fixtures
 * model a Run that finished with failures and is eligible for
 * `retry_failed`/`cancel`/`reconcile`, plus one still-`running` Run to prove
 * the recovery slot stays hidden before that point.
 */
export const PARTIAL_RUN_ID = 'seeded-partial-run';
export const PARTIAL_OPERATION_ID = 'operation-partial-run';
export const RUNNING_RUN_ID = 'seeded-running-run';
export const RECOVERY_PREVIEW_ID = 'recovery-preview-1';
export const EXPIRED_RECOVERY_PREVIEW_ID = 'recovery-preview-expired';

export const PARTIAL_RUN_ITEM_SUCCEEDED = {
  id: 'run-item-partial-1',
  ordinal: 1,
  state: 'succeeded',
  combination_fingerprint: 'fingerprint-partial-1',
  variant_index: 0,
  execution_mode: 'mock',
  error_summary: '',
  intent_kind: 'new_generation',
};

/** A failed item -- the reason `retry_failed`/`reconcile` are eligible on
 * `PARTIAL_RUN_DETAIL` below. */
export const PARTIAL_RUN_ITEM_FAILED = {
  id: 'run-item-partial-2',
  ordinal: 2,
  state: 'failed',
  combination_fingerprint: 'fingerprint-partial-2',
  variant_index: 1,
  execution_mode: 'mock',
  error_summary: 'Provider request timed out.',
  intent_kind: 'new_generation',
};

/** `GenerationRunDetail` for a Run that reached a terminal, partially-failed
 * state (`derive_run_state`'s `partially_succeeded`) -- carries
 * `allowed_actions` (`run_queries.run_actions`) so `RecoveryPanel` renders
 * `retry_failed` (allowed) and `resume_paused` (present but blocked, to
 * exercise the disabled + `reason_code` branch), plus `cancel` and
 * `reconcile` (both allowed). */
export const PARTIAL_RUN_DETAIL = {
  id: PARTIAL_RUN_ID,
  product_id: 'product-1',
  provider: 'openai',
  model: 'gpt-image-1',
  status: 'partially_succeeded',
  // `done` (1) + `failed` (2) < `total` (4) is deliberate: the remaining
  // item is `uncertain` -- this is what makes `RecoveryPanel`'s derived
  // `uncertainCount` (`ReconciliationGuidance`) non-zero *and* makes
  // `CancelConfirmation.hasInFlightItems` true (spec 3.8/3.11/3.12), unlike
  // a clean `done + failed === total` split which would leave both at
  // false/zero.
  total: 4,
  done: 1,
  failed: 2,
  execution_mode: 'mock',
  revision: 2,
  commands: [
    {
      id: 'command-partial-1',
      command_kind: 'submit',
      operation_id: PARTIAL_OPERATION_ID,
      created_at: '2026-09-01T12:00:00Z',
    },
  ],
  allowed_actions: [
    { action: 'retry_failed', allowed: true, reason_code: null },
    {
      action: 'resume_paused',
      allowed: false,
      reason_code: 'no_paused_items',
    },
    { action: 'cancel', allowed: true, reason_code: null },
    { action: 'reconcile', allowed: true, reason_code: null },
  ],
};

/** Terminal (matches `PARTIAL_RUN_DETAIL.status`), backing
 * `PARTIAL_RUN_DETAIL.commands[0]`. */
export const PARTIAL_OPERATION_DETAIL = {
  operation_id: PARTIAL_OPERATION_ID,
  command_type: 'submit_generation_run',
  state: 'partially_succeeded',
  result: { resource_type: 'generation_run', resource_id: PARTIAL_RUN_ID },
  created_at: '2026-09-01T12:00:00Z',
  updated_at: '2026-09-01T12:00:10Z',
};

/** Still-`running` Run -- `RUN_TERMINAL_STATES` excludes it, so
 * `RunProgressView` never mounts `recoverySlot` for it. No `commands`
 * (nothing submitted an Operation yet), matching a Run mid-execution. */
export const RUNNING_RUN_DETAIL = {
  id: RUNNING_RUN_ID,
  product_id: 'product-1',
  provider: 'openai',
  model: 'gpt-image-1',
  status: 'running',
  total: 4,
  done: 1,
  failed: 0,
  execution_mode: 'mock',
  revision: 1,
  commands: [],
};

/** `GenerationRecoveryPreview` (`purpose: 'run_recovery'`, spec:
 * `POST /generation/runs/{id}/recovery-previews`) for a `retry_failed_items`
 * request against `PARTIAL_RUN_DETAIL` -- targets the two `failed`/eligible
 * RunItems, `allowed_actions` gates the `RecoveryPreviewDisplay` "Confirm"
 * button (action id `"confirm"`, spec: `run_queries.recovery_preview_
 * actions`). */
export const RECOVERY_PREVIEW_DETAIL = {
  purpose: 'run_recovery',
  id: RECOVERY_PREVIEW_ID,
  preview_hash: 'recovery-preview-hash-1',
  command: 'retry_failed_items',
  targets: ['run-item-partial-2', 'run-item-partial-3'],
  estimated_additional_cost: { amount: '0.50', currency: 'USD' },
  valid_until: '2099-01-01T00:00:00Z',
  run_id: PARTIAL_RUN_ID,
  expected_run_revision: 2,
  allowed_actions: [{ action: 'confirm', allowed: true, reason_code: null }],
};

/** Same shape as `RECOVERY_PREVIEW_DETAIL` but already past `valid_until` --
 * used only by the 3.7 expired-preview spec, which overrides the
 * `recovery-previews` route for that one test via `page.route()` (last
 * registered wins) rather than changing the shared default every other
 * recovery spec reads. */
export const EXPIRED_RECOVERY_PREVIEW_DETAIL = {
  ...RECOVERY_PREVIEW_DETAIL,
  id: EXPIRED_RECOVERY_PREVIEW_ID,
  valid_until: '2020-01-01T00:00:00Z',
};

const PROBLEM_NOT_FOUND = {
  type: 'about:blank',
  title: 'Not Found',
  status: 404,
  detail: 'Generation draft not found.',
  instance: '/generation/drafts/' + NONEXISTENT_DRAFT_ID,
  code: 'generation_draft_not_found',
  trace_id: 'e2e-trace-not-found',
  retryable: false,
  field_errors: [],
};

async function json(route: Route, status: number, body: unknown) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

/**
 * Installs route interception for every network call the Slice 1 pages make
 * below the SSR boundary, so the specs in `draft-preview.spec.ts` run
 * without a live FastAPI domain API or seeded Product/ProductVersion data.
 *
 * Registration order matters: Playwright tries the most-recently-registered
 * matching `page.route()` handler first, so the generic
 * `generation/**` fallback is installed *before* the specific handlers below
 * it, letting the specific ones take priority while still covering any
 * incidental read (e.g. a sub-component of `DraftComposer` this task's
 * scope doesn't otherwise mock) with an inert empty-list response instead of
 * an unhandled, indefinitely-pending request.
 */
export async function mockGenerationBackend(page: Page): Promise<void> {
  await page.route('**/api/csrf', async (route) => {
    await json(route, 200, { token: 'e2e-csrf-token' });
  });

  // Fallback for any generation domain call this file doesn't stub
  // explicitly (e.g. dimension templates / reference assets the draft
  // composer's sub-components may query) — an empty, well-formed page
  // rather than a hung request.
  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/**`,
    async (route) => {
      if (route.request().method() === 'GET') {
        await json(route, 200, { items: [], next_cursor: null });
      } else {
        await json(route, 200, {});
      }
    }
  );

  // `list_products` (#162): the New Draft picker and every product-name
  // display read it; `product-1` is the product the drafts below use.
  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/products`,
    async (route) => {
      await json(route, 200, { items: [PRODUCT_SUMMARY] });
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/capabilities`,
    async (route) => {
      await json(route, 200, CAPABILITIES);
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/drafts`,
    async (route) => {
      const method = route.request().method();
      if (method === 'POST') {
        await json(route, 201, DRAFT_DETAIL);
      } else {
        await json(route, 200, { items: [DRAFT_SUMMARY], next_cursor: null });
      }
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/drafts/${NONEXISTENT_DRAFT_ID}`,
    async (route) => {
      await json(route, 404, PROBLEM_NOT_FOUND);
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/drafts/${DRAFT_ID}`,
    async (route) => {
      await json(route, 200, DRAFT_DETAIL);
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/drafts/${DRAFT_ID}/previews`,
    async (route) => {
      await json(route, 201, { id: PREVIEW_ID, draft_id: DRAFT_ID });
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/previews/${PREVIEW_ID}`,
    async (route) => {
      await json(route, 200, PREVIEW_DETAIL);
    }
  );

  // `submit_generation_run` (POST) and `list_generation_runs` (GET) share
  // the same `/generation/runs` BFF path (spec: `generated/paths.ts`),
  // exactly like `/generation/drafts` above.
  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/runs`,
    async (route) => {
      const method = route.request().method();
      if (method === 'POST') {
        await json(route, 202, {
          operation_id: OPERATION_ID,
          generation_run_id: RUN_ID,
        });
      } else {
        await json(route, 200, { items: [RUN_SUMMARY], next_cursor: null });
      }
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/runs/${RUN_ID}/items`,
    async (route) => {
      await json(route, 200, { items: [RUN_ITEM_DETAIL], next_cursor: null });
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/runs/${RUN_ID}`,
    async (route) => {
      await json(route, 200, RUN_DETAIL);
    }
  );

  // `get_operation` (spec §5.6) resolves under the workspace-level
  // `/operations/{id}` BFF path, not `/generation/**` -- outside the
  // fallback route above, so it needs its own explicit handler.
  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/operations/${OPERATION_ID}`,
    async (route) => {
      await json(route, 200, OPERATION_DETAIL);
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/runs/${PARTIAL_RUN_ID}`,
    async (route) => {
      await json(route, 200, PARTIAL_RUN_DETAIL);
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/runs/${PARTIAL_RUN_ID}/items`,
    async (route) => {
      await json(route, 200, {
        items: [PARTIAL_RUN_ITEM_SUCCEEDED, PARTIAL_RUN_ITEM_FAILED],
        next_cursor: null,
      });
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/operations/${PARTIAL_OPERATION_ID}`,
    async (route) => {
      await json(route, 200, PARTIAL_OPERATION_DETAIL);
    }
  );

  // `create_generation_run_recovery_preview` (`retry_failed`/`resume_paused`
  // -> `POST .../runs/{id}/recovery-previews`) -- retry/resume commands
  // themselves (`.../retry-failed-items`, `.../resume-paused-items`,
  // `.../cancel`, `.../reconcile`) fall through to the generic
  // `generation/**` 200-`{}` handler above: `RecoveryPanel` only inspects
  // `outcome.kind`, never the response body, for those four.
  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/runs/${PARTIAL_RUN_ID}/recovery-previews`,
    async (route) => {
      await json(route, 201, RECOVERY_PREVIEW_DETAIL);
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/runs/${RUNNING_RUN_ID}`,
    async (route) => {
      await json(route, 200, RUNNING_RUN_DETAIL);
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/runs/${RUNNING_RUN_ID}/items`,
    async (route) => {
      await json(route, 200, { items: [], next_cursor: null });
    }
  );
}

interface GenerationFixtures {
  // No-op override: forces the built-in `page` fixture to have
  // `mockGenerationBackend` installed before any test body runs, so specs
  // never need to call it manually.
  page: Page;
}

export const test = base.extend<GenerationFixtures>({
  // `provide` (not Playwright's usual `use`) — matches `e2e/library/
  // fixtures.ts`'s naming, which sidesteps an `eslint-plugin-react-hooks`
  // false positive: `react-hooks/rules-of-hooks` misreads a parameter
  // literally named `use` as a React Hook call.
  page: async ({ page }, provide) => {
    await mockGenerationBackend(page);
    await provide(page);
  },
});

export { expect };
