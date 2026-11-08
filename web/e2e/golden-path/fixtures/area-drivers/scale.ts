import { expect } from '@playwright/test';
import type { ScaleEvidence } from '../../artifacts/schemas';
import type { OwnerSessionCapability } from '../onboarding-harness';

export class ScaleDriver {
  private workspaceSlug: string;

  constructor(workspaceSlug: string) {
    this.workspaceSlug = workspaceSlug;
  }

  setWorkspaceSlug(workspaceSlug: string): void {
    this.workspaceSlug = workspaceSlug;
  }

  async verifyPersistedState(
    session: OwnerSessionCapability,
    evidence: ScaleEvidence
  ): Promise<void> {
    const page = session.page();
    const base = `/w/${this.workspaceSlug}`;

    await page.goto(`${base}/scale/${evidence.enrollment.runtimeId}`);
    await expect(
      page.getByRole('heading', {
        name: evidence.enrollment.runtimeId,
        exact: true,
      })
    ).toBeVisible({ timeout: 15_000 });

    const adviceResponse = await page.request.get(
      `/api/domain/v1/w/${this.workspaceSlug}/scale/customer_sandbox/` +
        `advice-threads?enrollment_id=${encodeURIComponent(evidence.enrollment.runtimeId)}`
    );
    expect(adviceResponse.ok()).toBe(true);
    const advicePayload = (await adviceResponse.json()) as {
      items?: Array<{ id: string }>;
    };
    expect(
      advicePayload.items?.some(
        (thread) => thread.id === evidence.adviceThread.runtimeId
      )
    ).toBe(true);

    const draftResponse = await page.request.get(
      `/api/domain/v1/w/${this.workspaceSlug}/generation/drafts/` +
        `${evidence.generationDraft.runtimeId}`
    );
    expect(draftResponse.ok()).toBe(true);
  }
}
