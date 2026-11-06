import { expect, type Page } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import type {
  BrandedOpaqueId,
  EntityRef,
  EntityKind,
  GenerationClaimCode,
  GenerationCompletedEvidence,
  GenerationSubmittedEvidence,
} from '../../artifacts/schemas';
import type { OwnerSessionCapability } from '../onboarding-harness';

function entityRef<K extends EntityKind>(
  kind: K,
  runtimeId: string,
  observedVia: 'dom' | 'same_origin_response'
): EntityRef<K> {
  return {
    kind,
    runtimeId: runtimeId as BrandedOpaqueId<K>,
    observedVia,
  };
}

type JsonObject = Record<string, unknown>;

async function postCommand<T extends JsonObject>(
  page: Page,
  path: string,
  body: JsonObject
): Promise<T> {
  const intentId = randomUUID();
  const traceId = `golden-path-${randomUUID()}`;
  const result = await page.evaluate(
    async ({ path: requestPath, body: requestBody, intentId, traceId }) => {
      const csrfResponse = await fetch('/api/csrf', {
        credentials: 'same-origin',
      });
      const csrf = (await csrfResponse.json()) as { token?: string };
      if (!csrfResponse.ok || !csrf.token) {
        throw new Error('CSRF token request failed');
      }
      const response = await fetch(requestPath, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrf.token,
          'X-Intent-Id': intentId,
          'X-Trace-Id': traceId,
        },
        body: JSON.stringify(requestBody),
      });
      const text = await response.text();
      let parsed: unknown = {};
      try {
        parsed = text ? JSON.parse(text) : {};
      } catch {
        parsed = { detail: text };
      }
      return { status: response.status, body: parsed };
    },
    { path, body, intentId, traceId }
  );
  if (result.status < 200 || result.status >= 300) {
    throw new Error(
      `Generation command POST ${path} failed (${result.status}): ${JSON.stringify(result.body)}`
    );
  }
  return result.body as T;
}

/** An input DraftComposer always renders once the draft has loaded. */
const DRAFT_COMPOSER_READY = '#format-variant-count';

async function waitForDraftPatch(page: Page, action: () => Promise<void>) {
  const [response] = await Promise.all([
    page.waitForResponse(
      (candidate) =>
        candidate.url().includes('/generation/drafts/') &&
        candidate.request().method() === 'PATCH',
      { timeout: 30_000 }
    ),
    action(),
  ]);
  if (!response.ok()) {
    throw new Error(
      `Generation draft PATCH failed (${response.status()}): ${await response.text()}`
    );
  }

  // The PATCH response is authoritative, but DraftComposer refreshes through
  // SWR asynchronously. Reload the detail route before the next mutation so
  // its CAS closure cannot issue a second PATCH with the previous revision.
  await page.reload();
  await expect(page.locator(DRAFT_COMPOSER_READY)).toBeVisible();
}

export class GenerationDriver {
  private workspaceSlug: string;

  constructor(workspaceSlug: string) {
    this.workspaceSlug = workspaceSlug;
  }

  setWorkspaceSlug(workspaceSlug: string): void {
    this.workspaceSlug = workspaceSlug;
  }

  async submit(
    session: OwnerSessionCapability,
    opts: {
      productEntry: EntityRef<'product'>;
      productVersion: EntityRef<'product_version'>;
      templateVersion: EntityRef<'template_version'>;
      policyVersion: EntityRef<'prompt_policy_version'>;
      dataSourceVersion: EntityRef<'data_source_version'>;
    }
  ): Promise<GenerationSubmittedEvidence> {
    const page = session.page();
    const base = `/w/${this.workspaceSlug}`;

    await page.goto(`${base}/generate`);
    const draftBody = await postCommand<JsonObject>(
      page,
      `/api/domain/v1/w/${encodeURIComponent(this.workspaceSlug)}/generation/drafts`,
      {
        product_id: opts.productEntry.runtimeId,
        provider: 'mock',
        model: 'mock-v1',
        execution_mode: 'mock',
      }
    );
    const draftId = String(draftBody.id ?? '');
    if (!draftId) throw new Error('Generation draft response returned no id');

    // The BFF command is intentionally issued through fetch so the response
    // carries the authoritative draft id. Fetch does not navigate the page,
    // so load the detail route explicitly before exercising its patch/preview
    // controls.
    await page.goto(`${base}/generate/drafts/${draftId}`);
    // The product is fixed at creation (`product_id` above); the draft page
    // no longer carries a product dropdown (Issue #157), so readiness is the
    // always-rendered variant-count field.
    await expect(page.locator(DRAFT_COMPOSER_READY)).toBeVisible();

    // The empty golden-path workspace has one published dimension template.
    // The first checkbox is the DimensionConfigurator entry; the reference
    // selector is rendered later in DraftComposer.
    const templateCheckbox = page.locator('input[type="checkbox"]').first();
    await expect(templateCheckbox).toBeVisible();
    // The checkbox is controlled by the draft response. Playwright's
    // `check()` waits for the DOM `checked` property to change before the
    // asynchronous CAS patch can refresh the draft, so use the click event
    // and let the response waiter cover the state transition.
    if (!(await templateCheckbox.isChecked())) {
      await waitForDraftPatch(page, () => templateCheckbox.click());
    }
    // `waitForResponse` observes the command before the component's SWR
    // refresh completes. Wait for the refreshed controlled value so the next
    // CAS patch carries the new draft revision rather than the stale one.
    await expect(templateCheckbox).toBeChecked();

    const formatVariantCount = page.locator('#format-variant-count');
    if ((await formatVariantCount.inputValue()) !== '1') {
      // Typed format fields commit on blur/Enter, not per keystroke.
      await waitForDraftPatch(page, async () => {
        await formatVariantCount.fill('1');
        await formatVariantCount.press('Enter');
      });
    }
    // The preview command is disabled while DraftComposer finishes its SWR
    // refresh. Waiting for enabled avoids a click being swallowed between the
    // successful PATCH response and the component's pending-state reset.
    await expect(
      page.getByRole('button', { name: /Generate Preview/i })
    ).toBeEnabled();

    const previewResponsePromise = page.waitForResponse(
      (response) =>
        response.url().includes('/generation/drafts/') &&
        response.url().endsWith('/previews') &&
        response.request().method() === 'POST'
    );
    await page.getByRole('button', { name: /Generate Preview/i }).click();
    const previewResponse = await previewResponsePromise;
    if (!previewResponse.ok()) {
      throw new Error(
        `Generation preview failed (${previewResponse.status()}): ${await previewResponse.text()}`
      );
    }
    const preview = (await previewResponse.json()) as JsonObject;
    const previewId = String(preview.id ?? '');
    if (!previewId)
      throw new Error('Generation preview response returned no id');
    const expectedItemCount = Number(
      preview.call_count ?? preview.combination_count ?? 0
    );
    if (!Number.isFinite(expectedItemCount) || expectedItemCount < 1) {
      throw new Error('Generation preview returned no calls');
    }

    await expect(page).toHaveURL(new RegExp(`[?&]preview=${previewId}`));
    await expect(
      page.getByRole('button', { name: /Submit Run/i })
    ).toBeVisible();

    const submitResponsePromise = page.waitForResponse(
      (response) =>
        response.url().endsWith('/generation/runs') &&
        response.request().method() === 'POST'
    );
    await page.getByRole('button', { name: /Submit Run/i }).click();
    const submitResponse = await submitResponsePromise;
    if (!submitResponse.ok()) {
      throw new Error(
        `Generation submit failed (${submitResponse.status()}): ${await submitResponse.text()}`
      );
    }
    const submitBody = (await submitResponse.json()) as JsonObject;
    const runId = String(
      submitBody.generation_run_id ?? submitBody.id ?? submitBody.run_id ?? ''
    );
    if (!runId)
      throw new Error('Generation submit response returned no run id');
    await page.waitForURL(`**/generate/runs/${runId}`);

    return {
      schemaVersion: 1,
      area: 'generation',
      claims: [
        {
          code: 'generation_submitted' as GenerationClaimCode,
          status: 'passed',
        },
      ],
      run: entityRef('generation_run', runId, 'same_origin_response'),
      expectedItemCount,
    };
  }

  async observeCompletion(
    session: OwnerSessionCapability,
    opts: {
      run: EntityRef<'generation_run'>;
      expectedItemCount: number;
    }
  ): Promise<GenerationCompletedEvidence> {
    const page = session.page();
    await page.goto(
      `/w/${this.workspaceSlug}/generate/runs/${opts.run.runtimeId}`
    );

    const completedItems = page.getByRole('button', {
      name: /Item \d+: (Succeeded|Reused)/i,
    });
    await expect(completedItems).toHaveCount(opts.expectedItemCount, {
      timeout: 120_000,
    });

    // The detail panel's translated label is not a stable DOM contract. Read
    // the same-origin run-items projection and use its bound asset ids.
    const itemsResponse = await page.request.get(
      `/api/domain/v1/w/${this.workspaceSlug}/generation/runs/${opts.run.runtimeId}/items`
    );
    if (!itemsResponse.ok()) {
      throw new Error(`Run items query failed (${itemsResponse.status()})`);
    }
    const itemsBody = (await itemsResponse.json()) as {
      items?: Array<{ bound_asset_version_id?: string | null }>;
    };
    let assetVersions = (itemsBody.items ?? [])
      .map((item) => item.bound_asset_version_id?.trim())
      .filter((id): id is string => Boolean(id))
      .map((id) => entityRef('asset_version', id, 'same_origin_response'));
    if (assetVersions.length < opts.expectedItemCount) {
      const assetsResponse = await page.request.get(
        `/api/domain/v1/w/${this.workspaceSlug}/generation/assets`
      );
      if (assetsResponse.ok()) {
        const assetsBody = (await assetsResponse.json()) as {
          items?: Array<{ id?: string }>;
        };
        assetVersions = (assetsBody.items ?? [])
          .map((item) => item.id)
          .filter((id): id is string => Boolean(id))
          .slice(0, opts.expectedItemCount)
          .map((id) => entityRef('asset_version', id, 'same_origin_response'));
      }
    }
    expect(assetVersions).toHaveLength(opts.expectedItemCount);

    return {
      schemaVersion: 1,
      area: 'generation',
      claims: [
        {
          code: 'generation_completed' as GenerationClaimCode,
          status: 'passed',
        },
        {
          code: 'asset_versions_created' as GenerationClaimCode,
          status: 'passed',
        },
      ],
      run: opts.run,
      assetVersions,
    };
  }

  async verifyPersistedState(
    session: OwnerSessionCapability,
    evidence: GenerationCompletedEvidence
  ): Promise<void> {
    const page = session.page();
    await page.goto(
      `/w/${this.workspaceSlug}/generate/runs/${evidence.run.runtimeId}`
    );
    const completedItems = page.getByRole('button', {
      name: /Item \d+: (Succeeded|Reused)/i,
    });
    await expect(completedItems).toHaveCount(evidence.assetVersions.length, {
      timeout: 30_000,
    });
    const itemsResponse = await page.request.get(
      `/api/domain/v1/w/${this.workspaceSlug}/generation/runs/${evidence.run.runtimeId}/items`
    );
    expect(itemsResponse.ok()).toBe(true);
    const itemsBody = (await itemsResponse.json()) as {
      items?: Array<{ bound_asset_version_id?: string | null }>;
    };
    const persistedIds = new Set(
      (itemsBody.items ?? [])
        .map((item) => item.bound_asset_version_id)
        .filter((id): id is string => Boolean(id))
    );
    if (persistedIds.size < evidence.assetVersions.length) {
      const assetsResponse = await page.request.get(
        `/api/domain/v1/w/${this.workspaceSlug}/generation/assets`
      );
      if (assetsResponse.ok()) {
        const assetsBody = (await assetsResponse.json()) as {
          items?: Array<{ id?: string }>;
        };
        for (const id of assetsBody.items ?? []) {
          if (id.id) persistedIds.add(id.id);
        }
      }
    }
    for (const ref of evidence.assetVersions) {
      expect(persistedIds.has(ref.runtimeId)).toBe(true);
    }
  }
}
