import { expect } from '@playwright/test';
import type {
  BrandedOpaqueId,
  EntityKind,
  EntityRef,
  ScaleClaimCode,
  ScaleEnrollmentEvidence,
  ScaleEvidence,
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

export class ScaleDriver {
  private workspaceSlug: string;

  constructor(workspaceSlug: string) {
    this.workspaceSlug = workspaceSlug;
  }

  setWorkspaceSlug(workspaceSlug: string): void {
    this.workspaceSlug = workspaceSlug;
  }

  async createAndActivateEnrollment(
    session: OwnerSessionCapability,
    opts: {
      winnerDecision: EntityRef<'winner_decision'>;
      winnerAssetVersion: EntityRef<'asset_version'>;
      offerVersion: EntityRef<'offer_version'>;
      market: string;
      placement: string;
      landingPage: string;
      campaign: EntityRef<'campaign'>;
      adSet: EntityRef<'ad_set'>;
      ad: EntityRef<'ad'>;
    }
  ): Promise<ScaleEnrollmentEvidence> {
    const page = session.page();
    const base = `/w/${this.workspaceSlug}`;

    await page.goto(`${base}/scale`);
    await page.waitForSelector('[data-testid="scale-workspace"]');

    await page.click('button:has-text("Create Enrollment")');
    await page.waitForSelector('[data-testid="enrollment-config"]');

    await this.selectOption(
      page,
      'winner-decision',
      opts.winnerDecision.runtimeId
    );
    await this.selectOption(
      page,
      'winner-asset',
      opts.winnerAssetVersion.runtimeId
    );
    await this.selectOption(page, 'offer', opts.offerVersion.runtimeId);
    await this.selectOption(page, 'campaign', opts.campaign.runtimeId);
    await this.selectOption(page, 'ad-set', opts.adSet.runtimeId);
    await this.selectOption(page, 'ad', opts.ad.runtimeId);

    await page.fill('input[name="market"], [aria-label="Market"]', opts.market);
    await page.fill(
      'input[name="placement"], [aria-label="Placement"]',
      opts.placement
    );
    await page.fill(
      'input[name="landing_page"], [aria-label="Landing page"]',
      opts.landingPage
    );

    const [createResponse] = await Promise.all([
      page.waitForResponse(
        (res) =>
          res.url().includes('/scale') &&
          res.url().includes('/enrollments') &&
          res.ok()
      ),
      page.click('button:has-text("Create"), button:has-text("Enroll")'),
    ]);

    const createBody = await createResponse.json();
    const enrollmentId =
      createBody.id || createBody.data?.id || createBody.enrollment_id;

    await page.waitForSelector('[data-testid="enrollment-created"]');

    await page.click(
      'button:has-text("Activate"), button:has-text("Activate Enrollment")'
    );

    await page.waitForSelector('[data-testid="enrollment-active"]', {
      timeout: 30_000,
    });

    return {
      schemaVersion: 1,
      area: 'scale',
      claims: [
        {
          code: 'scale_enrollment_created' as ScaleClaimCode,
          status: 'passed',
        },
      ],
      enrollment: entityRef(
        'scale_enrollment',
        enrollmentId,
        'same_origin_response'
      ),
    };
  }

  async observeAdviceAndConvertDraft(
    session: OwnerSessionCapability,
    opts: {
      enrollment: EntityRef<'scale_enrollment'>;
    }
  ): Promise<ScaleEvidence> {
    const page = session.page();
    const base = `/w/${this.workspaceSlug}`;

    await page.goto(`${base}/scale/${opts.enrollment.runtimeId}`);

    await page.waitForSelector(
      '[data-testid="advice-thread-ready"], [data-testid="advice-available"]',
      { timeout: 120_000 }
    );

    const adviceThreadEl = page
      .locator(
        '[data-entity-kind="advice_thread"], [data-testid="advice-thread"]'
      )
      .first();
    const adviceThreadId = await adviceThreadEl.getAttribute('data-entity-id');

    await page.click(
      'button:has-text("Convert to Draft"), button:has-text("Create Draft")'
    );

    const [draftResponse] = await Promise.all([
      page.waitForResponse((res) => res.url().includes('/drafts') && res.ok()),
      page.click('button:has-text("Confirm"), button:has-text("Convert")'),
    ]);

    const draftBody = await draftResponse.json();
    const draftId = draftBody.id || draftBody.data?.id || draftBody.draft_id;

    await page.waitForSelector(
      '[data-testid="draft-created"], [data-testid="generation-draft"]'
    );

    const submitButton = page.locator('button:has-text("Submit Draft")');
    if (await submitButton.isVisible()) {
      await expect(submitButton).toBeDisabled();
    }

    return {
      schemaVersion: 1,
      area: 'scale',
      claims: [
        {
          code: 'scale_advice_received' as ScaleClaimCode,
          status: 'passed',
        },
        {
          code: 'scale_draft_created' as ScaleClaimCode,
          status: 'passed',
        },
      ],
      enrollment: opts.enrollment,
      adviceThread: entityRef('advice_thread', adviceThreadId!, 'dom'),
      generationDraft: entityRef(
        'generation_draft',
        draftId,
        'same_origin_response'
      ),
    };
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

  private async selectOption(
    page: import('@playwright/test').Page,
    fieldName: string,
    versionId: string
  ): Promise<void> {
    const selector =
      `[data-testid="${fieldName}-select"], ` +
      `select[name="${fieldName}"], ` +
      `[aria-label="${fieldName}"]`;
    const el = page.locator(selector).first();
    const tagName = await el.evaluate((e) => e.tagName.toLowerCase());

    if (tagName === 'select') {
      await el.selectOption(versionId);
    } else {
      await el.click();
      await page.click(
        `[role="option"][data-value="${versionId}"], [role="option"]:has-text("${versionId}")`
      );
    }
  }
}
