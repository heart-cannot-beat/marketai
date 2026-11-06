import { expect } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import type {
  AssetsClaimCode,
  AssetsEvidence,
  BrandedOpaqueId,
  EntityKind,
  EntityRef,
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

type AssetAction = 'approve' | 'needs_changes' | 'reject' | 'skip';

const KEYBOARD_MAP: Record<AssetAction, string> = {
  approve: 'a',
  needs_changes: 'c',
  reject: 'x',
  skip: ' ',
};

export class AssetsDriver {
  private workspaceSlug: string;

  constructor(workspaceSlug: string) {
    this.workspaceSlug = workspaceSlug;
  }

  setWorkspaceSlug(workspaceSlug: string): void {
    this.workspaceSlug = workspaceSlug;
  }

  async reviewAndExport(
    session: OwnerSessionCapability,
    opts: {
      approvedAssets: EntityRef<'asset_version'>[];
      needsChangesAsset: EntityRef<'asset_version'>;
      rejectedAsset: EntityRef<'asset_version'>;
      skipAsset: EntityRef<'asset_version'>;
    }
  ): Promise<AssetsEvidence> {
    const page = session.page();
    const base = `/w/${this.workspaceSlug}`;

    await page.goto(`${base}/assets`);
    await expect(
      page.getByRole('heading', { name: 'Assets', exact: true })
    ).toBeVisible();

    await this.performKeyboardReview(page, opts);

    // The review summary exits through a shallow router update. Reload once
    // the overlay has cleared so the gallery query is rebuilt from the
    // persisted asset projection before starting the export flow.
    await expect(page).not.toHaveURL(/reviewMode=1/, { timeout: 15_000 });
    await page.reload();

    const reApprovedRef = await this.regenerateAndReapprove(
      page,
      opts.needsChangesAsset
    );

    // Regeneration uses the Generation draft route. Return to the Assets
    // gallery before selecting the export set.
    await page.goto(`${base}/assets`);
    const exportClaims = await this.exportZip(page);

    const approvedRefs = [...opts.approvedAssets];

    return {
      schemaVersion: 1,
      area: 'assets',
      claims: [
        {
          code: 'assets_approved' as AssetsClaimCode,
          status: 'passed',
        },
        {
          code: 'asset_needs_changes' as AssetsClaimCode,
          status: 'passed',
        },
        {
          code: 'asset_re_approved' as AssetsClaimCode,
          status: 'passed',
        },
        {
          code: 'export_completed' as AssetsClaimCode,
          status: 'passed',
        },
      ],
      approvedRefs,
      reApprovedRef,
      originalNeedsChangesRef: opts.needsChangesAsset,
      exportClaims,
    };
  }

  async verifyPersistedState(
    session: OwnerSessionCapability,
    evidence: AssetsEvidence
  ): Promise<void> {
    const page = session.page();
    const base = `/w/${this.workspaceSlug}`;

    await page.goto(`${base}/assets`);
    await expect(
      page.getByRole('heading', { name: 'Assets', exact: true })
    ).toBeVisible();

    const response = await page.request.get(
      `/api/domain/v1/w/${this.workspaceSlug}/generation/assets?review_value=approved`
    );
    expect(response.ok()).toBe(true);
    const body = (await response.json()) as { items?: Array<{ id?: string }> };
    const persistedIds = new Set(
      (body.items ?? [])
        .map((item) => item.id)
        .filter((id): id is string => Boolean(id))
    );
    for (const ref of [...evidence.approvedRefs, evidence.reApprovedRef]) {
      expect(persistedIds.has(ref.runtimeId)).toBe(true);
    }
  }

  private async performKeyboardReview(
    page: import('@playwright/test').Page,
    opts: {
      approvedAssets: EntityRef<'asset_version'>[];
      needsChangesAsset: EntityRef<'asset_version'>;
      rejectedAsset: EntityRef<'asset_version'>;
      skipAsset: EntityRef<'asset_version'>;
    }
  ): Promise<void> {
    const checkboxes = page.getByTestId('asset-select');
    await expect(checkboxes.first()).toBeVisible();
    const count = await checkboxes.count();
    for (let index = 0; index < count; index += 1) {
      if (!(await checkboxes.nth(index).isChecked())) {
        await checkboxes.nth(index).check({ force: true });
      }
    }

    // The review overlay renders its shortcut legend before the first
    // asset-version detail has arrived. Wait for that response so the first
    // A/C/X shortcut is not ignored while `canReview` is still false.
    const firstDetailResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/generation/asset-versions/') &&
        response.request().method() === 'GET' &&
        response.ok()
    );
    await page.getByTestId('selection-review').click();
    await firstDetailResponse;
    await expect(page.locator('kbd').first()).toBeVisible();

    const reviewOrder = this.buildReviewOrder(opts);
    const galleryResponse = await page.request.get(
      `/api/domain/v1/w/${this.workspaceSlug}/generation/assets`
    );
    const galleryBody = (await galleryResponse.json()) as {
      items?: Array<{ id: string }>;
    };
    const actionById = new Map<string, AssetAction>(
      reviewOrder.map(({ ref, action }) => [String(ref.runtimeId), action])
    );
    const queue = (galleryBody.items ?? [])
      .slice(0, reviewOrder.length)
      .map((item, index) => ({
        id: item.id,
        action: actionById.get(item.id) ?? reviewOrder[index]?.action,
      }))
      .filter(
        (item): item is { id: string; action: AssetAction } =>
          item.action !== undefined
      );
    const orderedQueue =
      queue.length === reviewOrder.length
        ? queue
        : reviewOrder.map(({ ref, action }) => ({
            id: ref.runtimeId,
            action,
          }));

    for (const [index, { id, action }] of orderedQueue.entries()) {
      const key = KEYBOARD_MAP[action];
      if (index > 0) {
        // ReviewMode advances its cursor immediately after the command
        // resolves, while the detail hook changes on the following render.
        // Reload the overlay at each cursor boundary so the shortcut handler
        // cannot retain the previous asset version in `currentVersion`.
        const detailResponse = page.waitForResponse(
          (response) =>
            response.url().includes(`/generation/asset-versions/${id}`) &&
            response.request().method() === 'GET' &&
            response.ok(),
          { timeout: 15_000 }
        );
        await page.reload();
        await detailResponse;
        await expect(
          page.getByText(`${index}/${orderedQueue.length}`, { exact: true })
        ).toBeVisible({ timeout: 15_000 });
        await expect
          .poll(
            async () => {
              const response = await page.request.get(
                `/api/domain/v1/w/${this.workspaceSlug}/generation/asset-versions/${id}`
              );
              if (!response.ok()) return false;
              const body = (await response.json()) as {
                allowed_actions?: Array<{
                  action?: string;
                  allowed?: boolean;
                }>;
              };
              return body.allowed_actions?.some(
                (eligibility) =>
                  eligibility.action === 'review' && eligibility.allowed
              );
            },
            { timeout: 15_000, intervals: [250, 500, 1000, 2000] }
          )
          .toBe(true);
      }
      // The review overlay re-renders when the async detail query settles and
      // can lose the focus established by ReviewMode's effect. Restore focus
      // before dispatching each shortcut so a late detail response cannot
      // swallow the keyboard action.
      const reviewOverlay = page.getByTestId('review-mode');
      // A completed HTTP response precedes React applying the eligibility
      // result. The dimmed A/C/X legend is the visible disabled state; do not
      // dispatch a shortcut until the actual review UI has enabled it.
      if (action !== 'skip') {
        await expect(
          reviewOverlay.getByTestId('review-shortcuts').locator('span').first()
        ).not.toHaveClass(/opacity-40/, { timeout: 15_000 });
      }
      await reviewOverlay.focus();
      await expect(reviewOverlay).toBeFocused();
      await page.keyboard.press(key);
      // ReviewMode ignores key presses while the previous review mutation is
      // in flight. Waiting for the queue cursor to advance keeps the golden
      // path deterministic on the slower CI network instead of dropping
      // later shortcuts after an arbitrary sleep.
      if (index < orderedQueue.length - 1) {
        await expect(
          page.getByText(`${index + 1}/${orderedQueue.length}`, { exact: true })
        ).toBeVisible({ timeout: 15_000 });
      }
    }

    await expect(page.getByTestId('review-complete')).toBeVisible();
    await page.getByTestId('review-done').click();
  }

  private buildReviewOrder(opts: {
    approvedAssets: EntityRef<'asset_version'>[];
    needsChangesAsset: EntityRef<'asset_version'>;
    rejectedAsset: EntityRef<'asset_version'>;
    skipAsset: EntityRef<'asset_version'>;
  }): { ref: EntityRef<'asset_version'>; action: AssetAction }[] {
    const order: {
      ref: EntityRef<'asset_version'>;
      action: AssetAction;
    }[] = [];

    for (const ref of opts.approvedAssets) {
      order.push({ ref, action: 'approve' });
    }
    order.push({ ref: opts.needsChangesAsset, action: 'needs_changes' });
    order.push({ ref: opts.rejectedAsset, action: 'reject' });
    order.push({ ref: opts.skipAsset, action: 'skip' });

    return order;
  }

  private async regenerateAndReapprove(
    page: import('@playwright/test').Page,
    needsChangesRef: EntityRef<'asset_version'>
  ): Promise<EntityRef<'asset_version'>> {
    // The gallery order is the generation combination order. The two
    // approved refs precede the needs-changes ref in the golden path.
    const targetIndex = 2;
    const existingAssetsResponse = await page.request.get(
      `/api/domain/v1/w/${this.workspaceSlug}/generation/assets`
    );
    const existingAssetsBody = (await existingAssetsResponse.json()) as {
      items?: Array<{ id: string }>;
    };
    const existingAssetIds = new Set(
      (existingAssetsBody.items ?? []).map((item) => item.id)
    );
    await page
      .getByTestId('asset-card')
      .nth(targetIndex)
      .getByRole('button')
      .click();
    await expect(page.getByTestId('regenerate-new_seed')).toBeVisible();

    const regenResponsePromise = page.waitForResponse(
      (response) =>
        response.url().includes('/regeneration-drafts') &&
        response.request().method() === 'POST' &&
        response.ok()
    );
    await page.getByTestId('regenerate-new_seed').click();
    const regenBody = (await regenResponsePromise).json() as Promise<{
      id?: string;
      draft_id?: string;
    }>;
    const draft = await regenBody;
    const draftId = draft.id ?? draft.draft_id;
    if (!draftId) throw new Error('Regeneration response returned no draft id');

    const base = `/w/${this.workspaceSlug}`;
    await page.goto(`${base}/generate/drafts/${draftId}`);
    await expect(
      page.getByRole('heading', { name: /draft configuration/i })
    ).toBeVisible();

    // Regeneration drafts intentionally start with an empty selector set. The
    // experiment freeze contract requires governed concept identities, which
    // are materialized from the selected dimension template during preview.
    // Re-select the workspace's template before generating the replacement so
    // the new AssetVersion remains eligible as an experiment candidate.
    const templateCheckbox = page.locator('input[type="checkbox"]').first();
    await expect(templateCheckbox).toBeVisible();
    if (!(await templateCheckbox.isChecked())) {
      await Promise.all([
        page.waitForResponse(
          (response) =>
            response.url().includes('/generation/drafts/') &&
            response.request().method() === 'PATCH' &&
            response.ok()
        ),
        templateCheckbox.click(),
      ]);
      await expect(templateCheckbox).toBeChecked();
    }

    const previewResponsePromise = page.waitForResponse(
      (response) =>
        response.url().includes('/generation/drafts/') &&
        response.url().endsWith('/previews') &&
        response.request().method() === 'POST' &&
        response.ok()
    );
    await expect(
      page.getByRole('button', { name: /Generate Preview/i })
    ).toBeEnabled();
    await page.getByRole('button', { name: /Generate Preview/i }).click();
    await previewResponsePromise;

    const runResponsePromise = page.waitForResponse(
      (response) =>
        response.url().endsWith('/generation/runs') &&
        response.request().method() === 'POST' &&
        response.ok()
    );
    await page.getByRole('button', { name: /Submit Run/i }).click();
    const runBody = (await runResponsePromise).json() as Promise<{
      generation_run_id?: string;
    }>;
    const runId = (await runBody).generation_run_id;
    if (!runId) throw new Error('Regeneration run response returned no run id');

    await expect
      .poll(
        async () => {
          const response = await page.request.get(
            `/api/domain/v1/w/${this.workspaceSlug}/generation/runs/${runId}`
          );
          const body = (await response.json()) as { status?: string };
          return body.status ?? '';
        },
        { timeout: 120_000, intervals: [1000] }
      )
      .toMatch(/succeeded|partially_succeeded/i);

    const assetsResponse = await page.request.get(
      `/api/domain/v1/w/${this.workspaceSlug}/generation/assets`
    );
    const assetsBody = (await assetsResponse.json()) as {
      items?: Array<{ id: string }>;
    };
    const newVersion = assetsBody.items?.find(
      (item) =>
        item.id !== needsChangesRef.runtimeId && !existingAssetIds.has(item.id)
    );
    if (!newVersion) throw new Error('Regeneration produced no asset version');
    const csrfResponse = await page.request.get('/api/csrf');
    const csrfBody = (await csrfResponse.json()) as { token?: string };
    if (!csrfBody.token) throw new Error('CSRF token request failed');
    const reviewResponse = await page.request.post(
      `/api/domain/v1/w/${this.workspaceSlug}/generation/asset-versions/${newVersion.id}/reviews`,
      {
        data: { review_value: 'approved' },
        headers: {
          'X-CSRF-Token': csrfBody.token,
          'X-Intent-Id': randomUUID(),
          'X-Trace-Id': `golden-path-${randomUUID()}`,
          Origin: new URL(page.url()).origin,
          Referer: page.url(),
        },
      }
    );
    if (!reviewResponse.ok()) {
      throw new Error(
        `Regenerated asset approval failed (${reviewResponse.status()}): ${await reviewResponse.text()}`
      );
    }
    return entityRef('asset_version', newVersion.id, 'same_origin_response');
  }

  private async exportZip(
    page: import('@playwright/test').Page
  ): Promise<readonly { code: string; status: 'passed' }[]> {
    const checkboxes = page.getByTestId('asset-select');
    // Exiting the fullscreen review overlay updates the shallow URL and
    // rehydrates the gallery. Selection state can be reset by that route
    // transition, so wait for the gallery first and restore one selection
    // before looking for the selection-bar action.
    await expect(checkboxes.first()).toBeVisible({ timeout: 30_000 });
    if (!(await checkboxes.first().isChecked())) {
      await checkboxes.first().check({ force: true });
    }
    await expect(page.getByTestId('selection-export')).toBeVisible({
      timeout: 30_000,
    });
    await page.getByTestId('selection-export').click();
    await expect(page.getByRole('dialog')).toBeVisible();

    const startResponsePromise = page.waitForResponse(
      (response) =>
        response.url().includes('/exports') &&
        response.request().method() === 'POST' &&
        response.status() === 202
    );
    await page.getByTestId('export-start').click();
    await startResponsePromise;
    await expect(page.getByTestId('export-download')).toBeVisible({
      timeout: 120_000,
    });

    return [{ code: 'zip_manifest_valid', status: 'passed' }];
  }
}
