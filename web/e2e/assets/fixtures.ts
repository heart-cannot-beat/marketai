import {
  test as base,
  test as setup,
  expect,
  type Page,
  type Route,
} from '@playwright/test';
import type { components } from '@/lib/domain-client/generated/domain-v1';

/**
 * Fixtures for the Assets workspace E2E coverage (Task 12,
 * `.superpowers/sdd/2026-09-08-assets-browser-ui/task-12-brief.md`).
 *
 * Mirrors `e2e/generation/fixtures.ts`'s shape: the page shell under test
 * (`pages/w/[slug]/assets.tsx`) is gated by `getWorkspacePageProps()`
 * (`lib/workspace-bootstrap.ts`), which resolves the Workspace and the
 * caller's session/membership server-side against Prisma/Postgres before
 * the page ever renders -- that SSR call cannot be intercepted from the
 * browser context Playwright drives, so these specs still depend on the
 * same `chromium` project auth this repo's other E2E suites use
 * (`storageState: 'e2e/.auth/owner.json'`, produced by
 * `e2e/workspace/fixtures.ts`'s `setup` project -- see
 * `playwright.config.ts`) and a seeded `workspace-a` the storage-state
 * owner belongs to.
 *
 * Everything *below* that SSR boundary is mocked here: every
 * `list_generation_assets` / `get_generation_asset_version` /
 * `list_generation_runs` / `create_generation_asset_review` /
 * `create_generation_regeneration_draft` /
 * `create_generation_asset_version_download_intent` /
 * `create_generation_export_package` / `get_generation_export_manifest` /
 * `create_generation_export_download_intent` call the Assets hooks make
 * (`hooks/domain/useDomainQuery.ts`, `hooks/domain/useDomainCommand.ts`,
 * `hooks/domain/useDomainPaginatedQuery.ts`) is a same-origin browser
 * `fetch` to `/api/domain/v1/w/:slug/generation/...`
 * (`lib/domain-client/gateway.ts`, `generated/paths.ts`) -- a request
 * Playwright's `page.route()` intercepts before it reaches the Next.js
 * server, so these specs need no live domain API or seeded GenerationAsset
 * rows. The one other network dependency, `GET /api/csrf`
 * (`lib/domain-client/csrf.ts`, required before any mutating call), is
 * stubbed the same way.
 *
 * `export { setup }` mirrors `e2e/generation/fixtures.ts`'s shape for the
 * `setup` Playwright project -- this file defines no additional
 * `setup(...)` cases of its own.
 *
 * The fixtures deliberately use the generated OpenAPI component types for
 * the payloads most likely to drift: AssetVersion detail, collection facets
 * and action eligibility, export package recovery, manifest entries, and
 * signed download grants.
 */
export { setup };

export const WORKSPACE_SLUG = 'workspace-a';
export const ASSETS_URL = `/w/${WORKSPACE_SLUG}/assets`;

export const RUN_ID = 'seeded-run-1';
export const VERSION_ID = 'seeded-asset-version-1';
export const VERSION_ID_2 = 'seeded-asset-version-2';
export const EXPORT_PACKAGE_ID = 'seeded-export-package-1';
export const EXPORT_OPERATION_ID = 'operation-export-1';

type AssetVersionSummary = components['schemas']['AssetVersionSummary'];
type AssetVersionDetail = components['schemas']['AssetVersionDetail'];
type AssetGalleryPage = components['schemas']['AssetGalleryPage'];
type AssetReview = components['schemas']['AssetReview'];
type ExportPackageQueued = components['schemas']['ExportPackageQueued'];
type ExportPackageDetail = components['schemas']['ExportPackageDetail'];
type ExportManifest = components['schemas']['ExportManifest'];
type DownloadGrant = components['schemas']['DownloadGrant'];
type OperationDetail = components['schemas']['OperationDetailResponse'];

/** Raw `list_generation_assets` item shape (`AssetVersionSummary`,
 * `hooks/assets/useAssetGallery.ts`) -- `AssetGrid`/`AssetCard` read this
 * directly, no mapping needed. */
export const ASSET_SUMMARY = {
  id: VERSION_ID,
  asset_id: 'seeded-asset-1',
  product_id: 'product-1',
  run_id: RUN_ID,
  provider: 'openai',
  model: 'gpt-image-1',
  status: 'succeeded',
  aspect_ratio: '1:1',
  stable_dimension_signature: 'sig-1',
  tags: {},
  effective_review_value: null,
  latest_qc_state: 'failed_blocking',
  contextual_conclusion: null,
  origin_kind: 'new_generation',
  execution_mode: 'byok',
  mock_evidence: null,
  byte_size: 204800,
  created_at: '2026-09-01T12:00:00Z',
} satisfies AssetVersionSummary;

/** A second item with a distinct id -- lets `deduplicateById`
 * (`useDomainPaginatedQuery.ts`) and infinite-scroll assertions see more
 * than one card, and carries `execution_mode: 'mock'` so the
 * Controlled Mock badge (`AssetCard`, Task 12 Step 3) has something to
 * render against. */
export const ASSET_SUMMARY_MOCK = {
  ...ASSET_SUMMARY,
  id: VERSION_ID_2,
  asset_id: 'seeded-asset-2',
  execution_mode: 'mock',
  effective_review_value: 'approved',
} satisfies AssetVersionSummary;

/** `AssetVersionDetail` (`get_generation_asset_version`) as
 * `LightboxModal`/`AssetSidebar`/`ReviewPanel`/`RegenerationPanel` read
 * it -- `allowed_actions` carries `review`/`regenerate`/`download` so both
 * panels render (`hooks/assets/useAssetActions.ts`). */
export const ASSET_DETAIL = {
  id: VERSION_ID,
  asset_id: 'seeded-asset-1',
  product_id: 'product-1',
  run_id: RUN_ID,
  provider: 'openai',
  model: 'gpt-image-1',
  status: 'succeeded',
  kind: 'image',
  aspect_ratio: '1:1',
  stable_dimension_signature: 'sig-1',
  tags: {},
  mime_type: 'image/png',
  byte_size: 204800,
  content_sha256: 'hash-1',
  seed: { requested: '42', effective: '42', unavailable_reason: null },
  prompt: 'A studio product photo of product-1.',
  producing_run_item_id: 'run-item-1',
  provider_attempt_id: 'provider-attempt-1',
  execution_mode: 'byok',
  origin_kind: 'new_generation',
  effective_review_value: null,
  review_head_id: null,
  reviews: [],
  qc_attempts: [
    {
      id: 'qc-attempt-1',
      state: 'failed_blocking',
      attempt_sequence: 1,
      feedback_round: 0,
      execution_mode: 'byok',
      created_at: '2026-09-01T12:00:30Z',
      summary: '外观与参考图不一致。',
      reasons: ['外壳是亮银色，参考图是哑光黑。'],
      consecutive_failures: 1,
      projected_review: 'needs_changes',
      model: 'gpt-5-mini',
      controlled_mock: false,
      checks: [
        {
          rule_id: 'must_match:mm-color',
          check_type: 'vlm',
          severity: 'must_match',
          outcome: 'fail',
          verdict: 'fail',
          reason: '外壳是亮银色，参考图是哑光黑。',
          fix_hint: 'keep the housing matte black',
          must_match_id: 'mm-color',
          description: 'Matte black housing',
        },
      ],
    },
  ],
  latest_qc_attempt_id: 'qc-attempt-1',
  latest_qc_state: 'failed_blocking',
  contextual_conclusions: [],
  parent: null,
  children: [{ version_id: VERSION_ID_2, intent: 'quality_feedback' }],
  mock_evidence: null,
  allowed_actions: [
    { action: 'review', allowed: true, reason_code: null, requirements: [] },
    {
      action: 'regenerate',
      allowed: true,
      reason_code: null,
      requirements: [],
    },
    { action: 'download', allowed: true, reason_code: null, requirements: [] },
  ],
  created_at: '2026-09-01T12:00:00Z',
} satisfies AssetVersionDetail;

/** Second `AssetVersionDetail`, backing `ASSET_SUMMARY_MOCK` -- distinct
 * `execution_mode`/`effective_review_value` so a deep-link to this id
 * (`gallery-navigation.spec.ts`) renders visibly different sidebar state
 * than `ASSET_DETAIL`. */
export const ASSET_DETAIL_2 = {
  ...ASSET_DETAIL,
  id: VERSION_ID_2,
  asset_id: 'seeded-asset-2',
  execution_mode: 'mock',
  effective_review_value: 'approved',
  review_head_id: 'review-existing-2',
  reviews: [
    {
      id: 'review-existing-2',
      asset_version_id: VERSION_ID_2,
      review_value: 'approved',
      reason: '',
      actor_id: 'owner-user-id',
      created_at: '2026-09-01T12:01:00Z',
      source: 'human',
    },
  ],
  qc_attempts: [],
  latest_qc_attempt_id: null,
  latest_qc_state: null,
  parent: { version_id: VERSION_ID, intent: 'quality_feedback' },
  children: [],
  mock_evidence: { scenario: 'default' },
} satisfies AssetVersionDetail;

/** Raw `list_generation_runs` item (`BatchRail`'s `RunSummary`). */
export const RUN_SUMMARY = {
  id: RUN_ID,
  product_id: 'product-1',
  provider: 'openai',
  model: 'gpt-image-1',
  status: 'succeeded',
  total: 2,
  done: 2,
  failed: 0,
  execution_mode: 'byok',
  created_at: '2026-09-01T12:00:00Z',
};

/** The product and test plan `RUN_SUMMARY` came from, so the batch rail
 * can name it「VoltNest 充电桩 的广告测试 · 样图」instead of its id
 * (Issue #191, `useRunOrigins` / `useProductDirectory`). */
export const PRODUCT_NAME = 'VoltNest 充电桩';
export const PLAN_ID = 'seeded-plan-1';
export const PLAN_SUMMARY = {
  plan_id: PLAN_ID,
  status: 'ready',
  revision: 1,
  product_id: 'product-1',
  product_name: PRODUCT_NAME,
  goal: 'explore',
  scale: 'sample',
  direction_count: 3,
  run_stages: ['sample'],
};
export const PLAN_DETAIL = {
  ...PLAN_SUMMARY,
  directions: [
    { direction_id: 'd1', title: '雨夜车库', removed: false },
    { direction_id: 'd2', title: '周末露营', removed: false },
    { direction_id: 'd3', title: '城市通勤', removed: true },
  ],
  runs: [
    {
      stage: 'sample',
      target_scale: 'sample',
      status: 'submitted',
      plan_revision: 1,
      run_id: RUN_ID,
      items_total: 2,
      items_done: 2,
      items_failed: 0,
    },
  ],
};

/** `AssetReview` (`create_generation_asset_review` 201 body). */
export const REVIEW_RESULT = {
  id: 'review-1',
  asset_version_id: VERSION_ID,
  review_value: 'approved',
  reason: '',
  actor_id: 'owner-user-id',
  created_at: '2026-09-01T12:05:00Z',
  source: 'human',
} satisfies AssetReview;

/** `GenerationDraftDetail` (`create_generation_regeneration_draft` 201
 * body) -- `RegenerationPanel` only reads `.draft_id` off the outcome, so
 * the remaining fields are filler. */
export const REGENERATION_DRAFT_RESULT = {
  id: 'regen-draft-1',
  draft_id: 'regen-draft-1',
  product_id: 'product-1',
  provider: 'openai',
  model: 'gpt-image-1',
  execution_mode: 'byok',
  state: 'draft',
  revision: 1,
  updated_at: '2026-09-01T12:05:00Z',
};

/** Async acknowledgement from `create_generation_export_package` (202). */
export const EXPORT_PACKAGE_QUEUED = {
  package_id: EXPORT_PACKAGE_ID,
  operation_id: EXPORT_OPERATION_ID,
  state: 'queued',
} satisfies ExportPackageQueued;

/** Recovery/polling projection from `get_generation_export_package`. */
export const EXPORT_PACKAGE_READY = {
  package_id: EXPORT_PACKAGE_ID,
  operation_id: EXPORT_OPERATION_ID,
  execution_mode: 'byok',
  state: 'ready',
  revision: 2,
  byte_size: 204800,
  zip_hash: 'sha256-abc123def456',
} satisfies ExportPackageDetail;

export const EXPORT_OPERATION_SUCCEEDED = {
  operation_id: EXPORT_OPERATION_ID,
  command_type: 'create_generation_export_package',
  state: 'succeeded',
  result: {
    resource_type: 'export_package',
    resource_id: EXPORT_PACKAGE_ID,
  },
  created_at: '2026-09-01T12:09:00Z',
  updated_at: '2026-09-01T12:10:00Z',
} satisfies OperationDetail;

/** `ExportManifest` (`get_generation_export_manifest`) -- ready, with one
 * deduplicated entry so `ExportManifestView`'s dedup summary (Task 11)
 * renders. */
export const EXPORT_MANIFEST_READY = {
  package_id: EXPORT_PACKAGE_ID,
  execution_mode: 'byok',
  total_versions: 2,
  entries: [
    {
      asset_version_id: VERSION_ID,
      asset_id: 'seeded-asset-1',
      content_sha256: 'hash-1',
      byte_size: 204800,
      mime_type: 'image/png',
      origin_kind: 'new_generation',
      execution_mode: 'byok',
      producing_run_item_id: 'run-item-1',
      provider_attempt_id: 'provider-attempt-1',
      requested_seed: '42',
      effective_seed: '42',
      seed_unavailable_reason: null,
    },
    {
      asset_version_id: VERSION_ID_2,
      asset_id: 'seeded-asset-2',
      content_sha256: 'hash-1',
      byte_size: 204800,
      mime_type: 'image/png',
      origin_kind: 'new_generation',
      execution_mode: 'byok',
      producing_run_item_id: 'run-item-2',
      provider_attempt_id: 'provider-attempt-2',
      requested_seed: '43',
      effective_seed: '43',
      seed_unavailable_reason: null,
    },
  ],
} satisfies ExportManifest;

/** `DownloadIntentResponse.download_grant` (`signed_get`) --
 * `url` is same-origin so `StorageOriginPolicy` allows it without extra
 * env configuration and `page.route()` below can intercept the transfer. */
export function downloadGrant(url: string): DownloadGrant {
  return {
    kind: 'signed_get',
    url,
    headers: {},
    content_disposition: 'attachment; filename="asset.png"',
  };
}

async function json(route: Route, status: number, body: unknown) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

/** A minimal valid PNG (1x1, transparent) -- enough for `<img>`/blob URL
 * assertions without a real asset fixture on disk. */
const ONE_PIXEL_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
  'base64'
);

/**
 * Installs route interception for every network call the Assets page
 * makes below the SSR boundary, so the specs in this directory run without
 * a live FastAPI domain API or seeded GenerationAsset/GenerationRun rows.
 *
 * Registration order matters: Playwright tries the most-recently-
 * registered matching `page.route()` handler first, so the generic
 * `generation/**` fallback is installed *before* the specific handlers
 * below it, letting the specific ones take priority while still covering
 * any incidental read this file doesn't stub explicitly with an inert
 * empty-list response instead of an unhandled, indefinitely-pending
 * request.
 */
export async function mockAssetsBackend(page: Page): Promise<void> {
  await page.route('**/api/csrf', async (route) => {
    await json(route, 200, { token: 'e2e-csrf-token' });
  });

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

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/assets*`,
    async (route) => {
      const page: AssetGalleryPage = {
        items: [ASSET_SUMMARY, ASSET_SUMMARY_MOCK],
        next_cursor: null,
        facets: {
          providers: [{ value: 'openai', count: 2 }],
          reviews: [
            { value: 'approved', count: 1 },
            { value: 'unreviewed', count: 1 },
          ],
          origins: [{ value: 'new_generation', count: 2 }],
          modes: [
            { value: 'byok', count: 1 },
            { value: 'mock', count: 1 },
          ],
          aspects: [{ value: '1:1', count: 2 }],
          qc_states: [],
          tags: [],
        },
        allowed_actions: [
          {
            action: 'review',
            allowed: true,
            reason_code: null,
            requirements: [],
          },
          {
            action: 'export',
            allowed: true,
            reason_code: null,
            requirements: [],
          },
        ],
      };
      await json(route, 200, page);
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/runs*`,
    async (route) => {
      if (route.request().method() === 'GET') {
        await json(route, 200, { items: [RUN_SUMMARY], next_cursor: null });
      } else {
        await route.fallback();
      }
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/products*`,
    async (route) => {
      await json(route, 200, {
        items: [{ product_id: 'product-1', name: PRODUCT_NAME }],
        next_cursor: null,
      });
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/test-plans*`,
    async (route) => {
      await json(route, 200, { items: [PLAN_SUMMARY], next_cursor: null });
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/test-plans/${PLAN_ID}`,
    async (route) => {
      await json(route, 200, PLAN_DETAIL);
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/asset-versions/${VERSION_ID}`,
    async (route) => {
      await json(route, 200, ASSET_DETAIL);
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/asset-versions/${VERSION_ID_2}`,
    async (route) => {
      await json(route, 200, ASSET_DETAIL_2);
    }
  );

  for (const versionId of [VERSION_ID, VERSION_ID_2]) {
    await page.route(
      `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/asset-versions/${versionId}/reviews`,
      async (route) => {
        const body = route.request().postDataJSON() as {
          review_value: string;
          reason?: string;
          supersedes_review_id?: string;
        };
        await json(route, 201, {
          ...REVIEW_RESULT,
          id: `review-${versionId}`,
          asset_version_id: versionId,
          review_value: body.review_value,
          reason: body.reason ?? '',
          supersedes_review_id: body.supersedes_review_id ?? null,
        });
      }
    );
  }

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/asset-versions/${VERSION_ID}/regeneration-drafts`,
    async (route) => {
      await json(route, 201, REGENERATION_DRAFT_RESULT);
    }
  );

  await page.route('**/e2e-blob/*.png', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'image/png',
      body: ONE_PIXEL_PNG,
    });
  });

  for (const versionId of [VERSION_ID, VERSION_ID_2]) {
    await page.route(
      `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/asset-versions/${versionId}/download-intent`,
      async (route, request) => {
        const url = new URL(request.url());
        await json(route, 201, {
          intent_id: `download-intent-${versionId}`,
          expires_at: '2026-09-01T12:15:00Z',
          download_grant: downloadGrant(
            `${url.origin}/e2e-blob/${versionId}.png`
          ),
        });
      }
    );
  }

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/exports`,
    async (route) => {
      if (route.request().method() === 'POST') {
        await json(route, 202, EXPORT_PACKAGE_QUEUED);
      } else {
        await route.fallback();
      }
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/exports/${EXPORT_PACKAGE_ID}`,
    async (route) => {
      await json(route, 200, EXPORT_PACKAGE_READY);
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/operations/${EXPORT_OPERATION_ID}`,
    async (route) => {
      await json(route, 200, EXPORT_OPERATION_SUCCEEDED);
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/exports/${EXPORT_PACKAGE_ID}/manifest`,
    async (route) => {
      await json(route, 200, EXPORT_MANIFEST_READY);
    }
  );

  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/exports/${EXPORT_PACKAGE_ID}/download-intent`,
    async (route, request) => {
      const url = new URL(request.url());
      await json(route, 201, {
        intent_id: `download-intent-${EXPORT_PACKAGE_ID}`,
        expires_at: '2026-09-01T12:15:00Z',
        download_grant: downloadGrant(`${url.origin}/e2e-blob/export.zip`),
      });
    }
  );

  await page.route('**/e2e-blob/export.zip', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/zip',
      body: Buffer.from('PK\x05\x06' + '\x00'.repeat(18)),
    });
  });
}

interface AssetsFixtures {
  // No-op override: forces the built-in `page` fixture to have
  // `mockAssetsBackend` installed before any test body runs, so specs
  // never need to call it manually. `use` (not Playwright's `use` param
  // name) is renamed `provide` to sidestep an `eslint-plugin-react-hooks`
  // false positive, matching `e2e/generation/fixtures.ts`.
  page: Page;
}

export const test = base.extend<AssetsFixtures>({
  page: async ({ page }, provide) => {
    await mockAssetsBackend(page);
    await provide(page);
  },
});

export { expect };
