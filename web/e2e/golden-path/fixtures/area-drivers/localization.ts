import { expect } from '@playwright/test';
import type { LocalizationOutcomesEvidence } from '../../artifacts/schemas';
import type { OwnerSessionCapability } from '../onboarding-harness';

export class LocalizationDriver {
  private workspaceSlug: string;

  constructor(workspaceSlug: string) {
    this.workspaceSlug = workspaceSlug;
  }

  setWorkspaceSlug(workspaceSlug: string): void {
    this.workspaceSlug = workspaceSlug;
  }

  async verifyPersistedState(
    session: OwnerSessionCapability,
    evidence: LocalizationOutcomesEvidence
  ): Promise<void> {
    const page = session.page();
    const base = `/w/${this.workspaceSlug}`;

    const batchId = evidence.batch.runtimeId;
    await page.goto(`${base}/localization?batch=${batchId}`);
    await expect(
      page.getByRole('heading', { name: 'Localization control room' })
    ).toBeVisible({ timeout: 15_000 });
    const response = await page.request.get(
      `/api/domain/v1/w/${this.workspaceSlug}/localization/batches/${batchId}`
    );
    expect(response.ok()).toBe(true);
    const batch = (await response.json()) as {
      id: string;
      items?: Array<{ id: string }>;
    };
    expect(batch.id).toBe(batchId);
    expect(batch.items?.length ?? 0).toBeGreaterThan(0);

    // The current workspace renders localized lineage inside the selected
    // item inspector rather than exposing the old data-entity test hooks.
    // Verify those durable IDs through the API and keep the page assertion
    // focused on the persisted batch route.
    for (const av of evidence.localizedAssetVersions) {
      const found = await Promise.all(
        (batch.items ?? []).map(async (item) => {
          const itemResponse = await page.request.get(
            `/api/domain/v1/w/${this.workspaceSlug}/localization/batches/${batchId}/items/${item.id}`
          );
          if (!itemResponse.ok()) return false;
          const detail = (await itemResponse.json()) as {
            lineage?: { localized_asset_version_id?: string | null };
          };
          return detail.lineage?.localized_asset_version_id === av.runtimeId;
        })
      );
      expect(found.some(Boolean)).toBe(true);
    }
  }
}
