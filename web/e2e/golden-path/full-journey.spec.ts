/**
 * Deterministic Playwright E2E scenario proving the complete v9 Beta
 * golden path on PostgreSQL (issue #60).
 *
 * Covers: invite → MFA/session → Workspace → controlled Mock setup →
 * Library → Generation → Asset review → Sandbox Experiment/PAUSED
 * Launch → Winner → Localization → Scale advice/draft.
 *
 * Survives browser refresh and targeted Web/API/Worker restarts with
 * no duplicate provider charges or remote objects after recovery.
 */

import { randomUUID } from 'crypto';
import {
  expect,
  test,
  readJson,
  domainMutationHeaders,
  totpCode,
  provisionWorkspace,
  installNetworkSpy,
  stopWorker,
  startWorker,
  runWorkerOnce,
  restartWorker,
  restartApiServer,
  restartWebService,
  libraryUrl,
  generateUrl,
  assetsUrl,
  experimentsUrl,
  localizationUrl,
  scaleUrl,
  scaleEnrollmentUrl,
  libraryBff,
  generationBff,
  launchBff,
  localizationBff,
  type WorkspaceProvisionResult,
} from './fixtures';
import {
  seedInfrastructure,
  seedLocalizationInfrastructure,
  seedScaleInfrastructure,
  seedExperimentMetrics,
  advanceManifest,
  inspectCanonicalSnapshot,
  inspectFakeMetaObjects,
  approveLocalizedAssetVersionThroughAssets,
  createLocalizationBatchThroughUi,
  prepareLocalizationBatchThroughUi,
  confirmLocalizationBatchThroughUi,
  type CanonicalSnapshot,
} from './domain-helpers';
import { en } from '../support/copy';

// ---------------------------------------------------------------------------
// Test-wide state, populated across steps
// ---------------------------------------------------------------------------

let ws: WorkspaceProvisionResult;
let prisma: any;
let domainDb: any;
let dispatchCurrentSnapshot: (
  teamId: string,
  dispatcherId: string
) => Promise<string>;

let ownerMfaSecret: string;
let ownerUserId: string;
let recoveryCodes: string[] = [];
let memberUserId: string;
let memberEmail: string;
let memberPassword: string;

/* eslint-disable @typescript-eslint/no-unused-vars --
   Step-scoped IDs: each is assigned in one step and read in a later step.
   Some remain write-only until the downstream step is wired up. */

// Library IDs
let productEntryId: string;
let productVersionId: string;
let offerEntryId: string;
let offerVersionId: string;
let templateEntryId: string;
let templateVersionId: string;
let policyEntryId: string;
let policyVersionId: string;
let personEntryId: string;
let personVersionId: string;
let dataSourceEntryId: string;
let dataSourceVersionId: string;

// Generation IDs
let generationDraftId: string;
let generationPreviewId: string;
let generationRunId: string;
let generationOperationId: string;

// Asset IDs
let assetVersionIds: string[] = [];
let approvedAssetVersionId: string;
let regeneratedDraftId: string;
let exportPackageId: string;

// Experiment IDs
let experimentId: string;
let experimentVersionId: string;
let candidateIds: string[] = [];
let decisionId: string;
let winnerAssetVersionId: string;

/* eslint-enable @typescript-eslint/no-unused-vars */

// Infra
let infraResult: ReturnType<typeof seedInfrastructure> extends infer T
  ? T
  : never;

// Localization
let batchId: string;

// Scale
let enrollmentId: string;
let episodeId: string;

// ---------------------------------------------------------------------------
// Provision the workspace (control-plane only, NO business objects)
// ---------------------------------------------------------------------------

const MFA_ENCRYPTION_KEY = Buffer.from(
  process.env.MFA_ENCRYPTION_KEY ??
    'Y29tcG9zZS1tZmEtZW5jcnlwdGlvbi1rZXktMzJiISE=',
  'base64'
);

const RUN_SUFFIX = randomUUID().slice(0, 8);

test.beforeAll(async () => {
  const provisioned = await provisionWorkspace({
    suffix: RUN_SUFFIX,
    mfaEncryptionKey: MFA_ENCRYPTION_KEY,
  });
  ws = provisioned.result;
  prisma = provisioned.prisma;
  domainDb = provisioned.domainDb;
  dispatchCurrentSnapshot = provisioned.dispatchCurrentSnapshot;
  ownerMfaSecret = ws.ownerMfaSecret;
});

test.afterAll(async () => {
  await prisma?.$disconnect();
  await domainDb?.$disconnect();
});

// ---------------------------------------------------------------------------
// THE SINGLE TEST
// ---------------------------------------------------------------------------

test('v9 Beta golden-path: empty Workspace to Scale advice', async ({
  page,
  browser,
}) => {
  test.setTimeout(900_000);
  const spy = installNetworkSpy(page);
  const slug = ws.workspaceSlug;

  // =========================================================================
  // STEP 1: Invite acceptance → account creation → sign-in
  // =========================================================================

  await test.step('Accept invitation and create owner account', async () => {
    await page.goto(
      `/auth/join?token=${encodeURIComponent(ws.invitationToken)}`
    );
    await page.locator('input[name="name"]').fill('Golden Path Owner');
    await page.locator('input[name="password"]').fill(ws.ownerPassword);
    await page.getByRole('button', { name: 'Create Account' }).click();
    await page.waitForURL(/\/auth\/login\?callbackUrl=/);
  });

  await test.step('Sign in with email and password', async () => {
    await page.locator('input[name="email"]').fill(ws.ownerEmail);
    await page.locator('input[name="password"]').fill(ws.ownerPassword);
    await page.getByRole('button', { name: /sign in/i }).click();
    await page.waitForURL(/\/settings\/security\?onboarding=/);

    const ownerUser = await prisma.user.findUnique({
      where: { email: ws.ownerEmail },
      select: { id: true },
    });
    expect(ownerUser).toBeTruthy();
    ownerUserId = ownerUser!.id;
  });

  // =========================================================================
  // STEP 2: MFA setup
  // =========================================================================

  await test.step('Set up MFA (TOTP)', async () => {
    await page.getByRole('button', { name: 'Set up MFA' }).click();

    ownerMfaSecret = (await page.locator('code').first().innerText()).trim();

    await page.getByLabel('Authenticator code').fill(totpCode(ownerMfaSecret));
    await page.getByRole('button', { name: 'Confirm MFA' }).click();

    await expect(
      page.getByText(/Save these one-time recovery codes/)
    ).toBeVisible();
    const codeElements = page
      .locator('li')
      .filter({ hasText: /^[A-F0-9-]{19}$/ });
    await expect(codeElements).toHaveCount(10);
    recoveryCodes = await codeElements.allTextContents();
    recoveryCodes = recoveryCodes.map((c) => c.trim());
  });

  // =========================================================================
  // STEP 2b: MFA recovery code test (separate browser context)
  // =========================================================================

  await test.step('Verify recovery code in separate browser context', async () => {
    expect(recoveryCodes.length).toBe(10);

    const recoveryContext = await browser.newContext({
      storageState: undefined,
      recordVideo: undefined,
    });
    const recoveryPage = await recoveryContext.newPage();
    recoveryPage.setDefaultTimeout(15_000);
    recoveryPage.setDefaultNavigationTimeout(15_000);

    try {
      await recoveryPage.goto('/auth/login');
      await recoveryPage.locator('input[name="email"]').fill(ws.ownerEmail);
      await recoveryPage
        .locator('input[name="password"]')
        .fill(ws.ownerPassword);
      await recoveryPage
        .locator('input[name="mfaCode"]')
        .fill(recoveryCodes[0]);
      await recoveryPage.getByRole('button', { name: /sign in/i }).click();

      await expect(recoveryPage).toHaveURL(/\/w\/|\/settings/, {
        timeout: 15_000,
      });
    } finally {
      await recoveryContext.close();
    }
  });

  // =========================================================================
  // STEP 3: Entitlement dispatch → workspace activation
  // =========================================================================

  await test.step('Dispatch entitlement and activate workspace', async () => {
    const initialDispatcherId = `e2e-gp-initial-${RUN_SUFFIX}`;
    await dispatchCurrentSnapshot(ws.workspaceId, initialDispatcherId);

    await expect
      .poll(async () => {
        const team = await prisma.team.findUnique({
          where: { id: ws.workspaceId },
        });
        return team?.lifecycle;
      })
      .toBe('ACTIVE');

    const activeDispatcherId = `e2e-gp-active-${RUN_SUFFIX}`;
    await dispatchCurrentSnapshot(ws.workspaceId, activeDispatcherId);

    await page.getByRole('link', { name: 'Continue to Workspace' }).click();
    await page.waitForURL(new RegExp(`/w/${slug}`));
    await expect(page.locator('#workspace-selector')).toHaveValue(slug, {
      timeout: 30_000,
    });
  });

  // =========================================================================
  // STEP 4: Seed platform infrastructure (controlled Mock only)
  // =========================================================================

  await test.step('Seed controlled-Mock platform infrastructure', async () => {
    infraResult = seedInfrastructure(ws.workspaceId, slug, ownerUserId);
    expect(infraResult.controlledMockConnectionId).toBeTruthy();
    expect(infraResult.controlledMockScenarioVersionId).toBeTruthy();
  });

  // =========================================================================
  // STEP 5: Library — create Product, Offer, template, policy through UI
  // =========================================================================

  await test.step('Create and publish Product through UI', async () => {
    await page.goto(libraryUrl(slug));
    await expect(page.getByRole('heading', { name: /library/i })).toBeVisible({
      timeout: 15_000,
    });

    // Select product category
    await page
      .getByRole('button', { name: /product/i })
      .first()
      .click();

    // Create product entry
    const createBtn = page.getByRole('button', { name: /create|new/i }).first();
    await createBtn.click();
    await page.getByLabel(/display name|name/i).fill('GP E2E Product');
    const entryResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/library/entries') &&
        r.request().method() === 'POST' &&
        r.status() === 201,
      { timeout: 10_000 }
    );
    await page
      .getByRole('button', { name: /create/i })
      .filter({ hasNotText: /cancel/i })
      .click();

    await expect(
      page.getByRole('heading', { name: 'GP E2E Product' })
    ).toBeVisible({
      timeout: 10_000,
    });

    const entryResponse = await entryResponsePromise;
    const entryBody = (await entryResponse.json()) as { id: string };
    productEntryId = entryBody.id;

    // Save draft with all required content fields via BFF (with CAS headers)
    const productDraftResponse = await page.request.put(
      `${libraryBff(slug)}/entries/${productEntryId}/draft`,
      {
        headers: await domainMutationHeaders(page, '"1"'),
        data: {
          content: {
            name: 'GP E2E Product',
            description: 'A product for golden-path E2E validation',
            audience: 'Adults 25-54, tech-savvy early adopters',
            selling_points: ['High quality', 'Affordable price'],
            must_match: [
              {
                id: 'gp-product-visible',
                type: 'visual',
                description: 'The product remains clearly visible',
                severity: 'blocking',
                viewpoints: ['front'],
                qc_method: 'human_review',
                confirmed: true,
              },
            ],
          },
        },
      }
    );
    expect(productDraftResponse.ok()).toBe(true);
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'GP E2E Product' })
    ).toBeVisible({
      timeout: 10_000,
    });

    // Publish — MFA is checked only at login (ADR 0015), so no code here.
    const publishBtn = page.locator('[data-command="publish"]');
    await expect(publishBtn).toBeEnabled({ timeout: 5_000 });
    await publishBtn.click();

    // Confirm publish in dialog
    const confirmPublish = page
      .getByRole('dialog', { name: /publish entry/i })
      .getByRole('button', { name: /^publish$/i });
    await expect(confirmPublish).toBeEnabled({ timeout: 10_000 });
    const publishResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/publish') &&
        r.request().method() === 'POST' &&
        r.status() === 201,
      { timeout: 15_000 }
    );
    await confirmPublish.click();

    const publishResponse = await publishResponsePromise;
    const version = (await publishResponse.json()) as {
      id: string;
      state: string;
    };
    productVersionId = version.id;
    expect(version.state).toBe('effective');
  });

  await test.step('Create and publish Offer under Product', async () => {
    // Navigate to product's internal entries
    const internalBtn = page.getByRole('button', {
      name: /offer|add.*internal|new.*offer/i,
    });
    await expect(internalBtn).toBeVisible({ timeout: 5_000 });
    await internalBtn.click();

    await page.getByLabel(/display name|name/i).fill('GP E2E Offer');
    await page
      .getByRole('button', { name: /create/i })
      .filter({ hasNotText: /cancel/i })
      .click();

    await expect(
      page.getByRole('heading', { name: 'GP E2E Offer' })
    ).toBeVisible({
      timeout: 10_000,
    });

    // Get offer entry ID
    const listResponse = await page.request.get(
      `${libraryBff(slug)}/entries/${productEntryId}/internal?entry_type=offer`
    );
    const internals = await readJson<{ items: Array<{ id: string }> }>(
      listResponse
    );
    offerEntryId = internals.items[0].id;

    // Save draft with all required Offer content fields via BFF (with CAS headers)
    const nextWeekDate = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000);
    const offerDraftResponse = await page.request.put(
      `${libraryBff(slug)}/entries/${offerEntryId}/draft`,
      {
        headers: await domainMutationHeaders(page, '"1"'),
        data: {
          content: {
            product_version_id: productVersionId,
            item: { sku: 'GP-E2E-001', quantity: 1 },
            landing_page_url: 'https://example.com/gp-e2e-offer',
            market_scope: ['US', 'GB'],
            valid_from: nextWeekDate.toISOString().slice(0, 10),
            source_market_snapshot: {
              locale: 'en-US',
              market: 'US',
              placement: 'feed',
              country: 'US',
              currency: 'USD',
            },
          },
        },
      }
    );
    expect(offerDraftResponse.ok()).toBe(true);
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'GP E2E Offer' })
    ).toBeVisible({
      timeout: 10_000,
    });

    // Publish offer
    const publishBtn = page.locator('[data-command="publish"]');
    await expect(publishBtn).toBeEnabled({ timeout: 5_000 });
    await publishBtn.click();

    const confirmPublish = page
      .getByRole('dialog', { name: /publish entry/i })
      .getByRole('button', { name: /^publish$/i });
    await expect(confirmPublish).toBeEnabled({ timeout: 10_000 });
    const publishResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/publish') &&
        r.request().method() === 'POST' &&
        r.status() === 201,
      { timeout: 15_000 }
    );
    await confirmPublish.click();

    const publishResponse = await publishResponsePromise;
    const version = (await publishResponse.json()) as {
      id: string;
      state: string;
    };
    offerVersionId = version.id;
    expect(version.state).toBe('effective');
  });

  await test.step('Create and publish Dimension Template', async () => {
    await page.goto(libraryUrl(slug));

    await page
      .getByRole('button', { name: /template/i })
      .first()
      .click();
    const createBtn = page.getByRole('button', { name: /create|new/i }).first();
    await createBtn.click();
    await page.getByLabel(/display name|name/i).fill('GP E2E Template');
    await page
      .getByRole('button', { name: /create/i })
      .filter({ hasNotText: /cancel/i })
      .click();

    await expect(
      page.getByRole('heading', { name: 'GP E2E Template' })
    ).toBeVisible({
      timeout: 10_000,
    });

    // Get template entry ID
    const listResponse = await page.request.get(
      `${libraryBff(slug)}/entries?entry_type=dimension_template`
    );
    const entries = await readJson<{ items: Array<{ id: string }> }>(
      listResponse
    );
    templateEntryId = entries.items[0].id;

    // Save draft with structured dimensions via BFF (with CAS headers)
    const templateDraftResponse = await page.request.put(
      `${libraryBff(slug)}/entries/${templateEntryId}/draft`,
      {
        headers: await domainMutationHeaders(page, '"1"'),
        data: {
          content: {
            combination_cardinality: 'one_per_dimension',
            dimensions: [
              {
                id: 'format',
                name: 'Format',
                values: [
                  {
                    id: 'square',
                    positive_fragments: ['1:1 square format'],
                    negative_fragments: [],
                  },
                  {
                    id: 'portrait',
                    positive_fragments: ['4:5 portrait format'],
                    negative_fragments: [],
                  },
                  {
                    id: 'landscape',
                    positive_fragments: ['16:9 landscape format'],
                    negative_fragments: [],
                  },
                ],
              },
            ],
          },
        },
      }
    );
    expect(templateDraftResponse.ok()).toBe(true);
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'GP E2E Template' })
    ).toBeVisible({
      timeout: 10_000,
    });

    // Publish
    const publishBtn = page.locator('[data-command="publish"]');
    await expect(publishBtn).toBeEnabled({ timeout: 5_000 });
    await publishBtn.click();

    const confirmPublish = page
      .getByRole('dialog', { name: /publish entry/i })
      .getByRole('button', { name: /^publish$/i });
    await expect(confirmPublish).toBeEnabled({ timeout: 10_000 });
    const publishResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/publish') &&
        r.request().method() === 'POST' &&
        r.status() === 201,
      { timeout: 15_000 }
    );
    await confirmPublish.click();

    const publishResponse = await publishResponsePromise;
    const version = (await publishResponse.json()) as { id: string };
    templateVersionId = version.id;
  });

  await test.step('Create and publish Prompt Policy', async () => {
    await page.goto(libraryUrl(slug));

    await page
      .getByRole('button', { name: /policy|prompt/i })
      .first()
      .click();
    const createBtn = page.getByRole('button', { name: /create|new/i }).first();
    await createBtn.click();
    await page.getByLabel(/display name|name/i).fill('GP E2E Policy');
    const policyCreatePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/library/entries') &&
        r.request().method() === 'POST' &&
        r.status() === 201,
      { timeout: 10_000 }
    );
    await page
      .getByRole('button', { name: /create/i })
      .filter({ hasNotText: /cancel/i })
      .click();

    await expect(
      page.getByRole('heading', { name: 'GP E2E Policy' })
    ).toBeVisible({
      timeout: 10_000,
    });

    const policyCreateResponse = await policyCreatePromise;
    const policyBody = (await policyCreateResponse.json()) as { id: string };
    policyEntryId = policyBody.id;

    const policyDraftResponse = await page.request.put(
      `${libraryBff(slug)}/entries/${policyEntryId}/draft`,
      {
        headers: await domainMutationHeaders(page, '"1"'),
        data: {
          content: {
            registered_tokens: [
              { name: 'product_name' },
              { name: 'offer_description' },
            ],
          },
        },
      }
    );
    expect(policyDraftResponse.ok()).toBe(true);
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'GP E2E Policy' })
    ).toBeVisible({
      timeout: 10_000,
    });

    // Publish
    const publishBtn = page.locator('[data-command="publish"]');
    await expect(publishBtn).toBeEnabled({ timeout: 5_000 });
    await publishBtn.click();

    const confirmPublish = page
      .getByRole('dialog', { name: /publish entry/i })
      .getByRole('button', { name: /^publish$/i });
    await expect(confirmPublish).toBeEnabled({ timeout: 10_000 });
    const publishResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/publish') &&
        r.request().method() === 'POST' &&
        r.status() === 201,
      { timeout: 15_000 }
    );
    await confirmPublish.click();

    const publishResponse = await publishResponsePromise;
    const version = (await publishResponse.json()) as { id: string };
    policyVersionId = version.id;
  });

  await test.step('Create and publish Person entry', async () => {
    await page.goto(libraryUrl(slug));

    await page
      .getByRole('button', { name: /people/i })
      .first()
      .click();
    const createBtn = page.getByRole('button', { name: /create|new/i }).first();
    await createBtn.click();
    await page.getByLabel(/display name|name/i).fill('GP E2E Person');
    const personCreatePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/library/entries') &&
        r.request().method() === 'POST' &&
        r.status() === 201,
      { timeout: 10_000 }
    );
    await page
      .getByRole('button', { name: /create/i })
      .filter({ hasNotText: /cancel/i })
      .click();

    await expect(
      page.getByRole('heading', { name: 'GP E2E Person' })
    ).toBeVisible({
      timeout: 10_000,
    });

    const personCreateResponse = await personCreatePromise;
    const personBody = (await personCreateResponse.json()) as { id: string };
    personEntryId = personBody.id;

    const personDraftResponse = await page.request.put(
      `${libraryBff(slug)}/entries/${personEntryId}/draft`,
      {
        headers: await domainMutationHeaders(page, '"1"'),
        data: {
          content: {
            name: 'GP E2E Person',
            person_type: 'fictional',
            age_expression: '30-35',
            rights: {
              regions: ['US', 'GB'],
              channels: ['social', 'display'],
            },
          },
        },
      }
    );
    expect(personDraftResponse.ok()).toBe(true);
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'GP E2E Person' })
    ).toBeVisible({
      timeout: 10_000,
    });

    const publishBtn = page.locator('[data-command="publish"]');
    await expect(publishBtn).toBeEnabled({ timeout: 5_000 });
    await publishBtn.click();

    const confirmPublish = page
      .getByRole('dialog', { name: /publish entry/i })
      .getByRole('button', { name: /^publish$/i });
    await expect(confirmPublish).toBeEnabled({ timeout: 10_000 });
    const publishResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/publish') &&
        r.request().method() === 'POST' &&
        r.status() === 201,
      { timeout: 15_000 }
    );
    await confirmPublish.click();

    const publishResponse = await publishResponsePromise;
    const version = (await publishResponse.json()) as { id: string };
    personVersionId = version.id;
  });

  await test.step('Create and publish Data Source entry', async () => {
    await page.goto(libraryUrl(slug));

    await page
      .getByRole('button', { name: /data.*source/i })
      .first()
      .click();
    const createBtn = page.getByRole('button', { name: /create|new/i }).first();
    await createBtn.click();
    await page.getByLabel(/display name|name/i).fill('GP E2E Data Source');
    const dsCreatePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/library/entries') &&
        r.request().method() === 'POST' &&
        r.status() === 201,
      { timeout: 10_000 }
    );
    await page
      .getByRole('button', { name: /create/i })
      .filter({ hasNotText: /cancel/i })
      .click();

    await expect(
      page.getByRole('heading', { name: 'GP E2E Data Source' })
    ).toBeVisible({
      timeout: 10_000,
    });

    const dsCreateResponse = await dsCreatePromise;
    const dsBody = (await dsCreateResponse.json()) as { id: string };
    dataSourceEntryId = dsBody.id;

    const dsDraftResponse = await page.request.put(
      `${libraryBff(slug)}/entries/${dataSourceEntryId}/draft`,
      {
        headers: await domainMutationHeaders(page, '"1"'),
        data: {
          content: {
            adapter_kind: 'mock',
            execution_mode: 'mock',
          },
        },
      }
    );
    expect(dsDraftResponse.ok()).toBe(true);
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'GP E2E Data Source' })
    ).toBeVisible({
      timeout: 10_000,
    });

    const publishBtn = page.locator('[data-command="publish"]');
    await expect(publishBtn).toBeEnabled({ timeout: 5_000 });
    await publishBtn.click();

    const confirmPublish = page
      .getByRole('dialog', { name: /publish entry/i })
      .getByRole('button', { name: /^publish$/i });
    await expect(confirmPublish).toBeEnabled({ timeout: 10_000 });
    const publishResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/publish') &&
        r.request().method() === 'POST' &&
        r.status() === 201,
      { timeout: 15_000 }
    );
    await confirmPublish.click();

    const publishResponse = await publishResponsePromise;
    const version = (await publishResponse.json()) as { id: string };
    dataSourceVersionId = version.id;
  });

  // =========================================================================
  // STEP 5a-post: Import manual facts into DataSource
  // =========================================================================

  await test.step('Import manual facts into DataSource entry', async () => {
    await page.getByRole('tab', { name: /^Facts$/i }).click();
    // #54: every row names its Offer. The rows below name none, so the
    // import gives them the Offer published above as its default.
    await page
      .getByLabel(en('library', 'data-source-ops.default-offer-label'))
      .selectOption({ label: 'GP E2E Offer' });
    await page.getByLabel(/select fact file to import/i).setInputFiles({
      name: 'golden-path-facts.json',
      mimeType: 'application/json',
      buffer: Buffer.from(
        JSON.stringify({
          facts: [
            {
              fact_id: 'gp-price-us',
              fact_type: 'price',
              country_code: 'US',
              valid_from: '2026-01-01',
              valid_to: '2099-12-31',
              amount: '19.99',
              currency: 'USD',
              evidence_source: 'Acme Charge US price list',
            },
            {
              // A market the Offer sells in (market_scope US, GB).
              fact_id: 'gp-delivery-gb',
              fact_type: 'delivery',
              country_code: 'GB',
              valid_from: '2026-01-01',
              valid_to: '2099-12-31',
              delivery_days_min: 2,
              delivery_days_max: 5,
              region: 'GB',
              evidence_source: 'Acme Charge GB carrier rates',
            },
          ],
        })
      ),
    });
    await expect(
      page.getByText(en('library', 'data-source-ops.import-complete'))
    ).toBeVisible({ timeout: 120_000 });

    // #23: the import checks every row and stores the report on the draft
    // it opens; these rows, from a published data source and for an Offer
    // that sells in their markets, pass.
    await page
      .getByRole('button', {
        name: en('library', 'data-source-ops.open-fact-set'),
      })
      .click();
    await expect(
      page.getByText(en('library', 'draft.validation-passed'))
    ).toBeVisible({ timeout: 30_000 });
  });

  // =========================================================================
  // STEP 5b: Member invitation acceptance
  // =========================================================================

  await test.step('Invite and onboard a member', async () => {
    memberEmail = `gp-member-${RUN_SUFFIX}@e2e.test`;
    memberPassword = `MemberPw!${RUN_SUFFIX}`;

    const memberToken = randomUUID();
    const invitation = await prisma.invitation.create({
      data: {
        teamId: ws.workspaceId,
        email: memberEmail,
        role: 'MEMBER',
        invitedBy: ownerUserId,
        token: memberToken,
        kind: 'MEMBER',
        expires: new Date(Date.now() + 7 * 24 * 60 * 60 * 1000),
      },
    });

    const memberContext = await browser.newContext({
      storageState: undefined,
      recordVideo: undefined,
    });
    const memberPage = await memberContext.newPage();

    try {
      await memberPage.goto(
        `/auth/join?token=${encodeURIComponent(invitation.token)}`
      );
      await memberPage.locator('input[name="name"]').fill('GP Member');
      await memberPage.locator('input[name="password"]').fill(memberPassword);
      await memberPage.getByRole('button', { name: 'Create Account' }).click();
      await memberPage.waitForURL(/\/auth\/login/, { timeout: 15_000 });

      await memberPage.locator('input[name="email"]').fill(memberEmail);
      await memberPage.locator('input[name="password"]').fill(memberPassword);
      await memberPage.getByRole('button', { name: /sign in/i }).click();

      await expect(memberPage).toHaveURL(new RegExp(`/w/${slug}|/settings`), {
        timeout: 30_000,
      });

      const memberUser = await prisma.user.findUnique({
        where: { email: memberEmail },
        select: { id: true },
      });
      expect(memberUser).toBeTruthy();
      memberUserId = memberUser!.id;

      const membership = await prisma.teamMember.findFirst({
        where: {
          teamId: ws.workspaceId,
          userId: memberUserId,
        },
      });
      expect(membership).toBeTruthy();
      expect(membership!.role).toBe('MEMBER');

      // Verify member scope: member can see the workspace library but not publish
      const memberLibResponse = await memberPage.request.get(
        `${libraryBff(slug)}/entries`
      );
      expect(memberLibResponse.status()).toBe(200);
    } finally {
      await memberContext.close();
    }
  });

  // =========================================================================
  // STEP 6: Generation — draft → preview → submit → Worker
  // =========================================================================

  await test.step('Create Generation draft through UI', async () => {
    await page.goto(generateUrl(slug));
    // Both checks name one element whether or not the drafts list has loaded
    // yet; the empty state adds its own copy and action once it has.
    await expect(
      page.getByRole('heading', {
        level: 1,
        name: 'New Generation',
        exact: true,
      })
    ).toBeVisible({ timeout: 15_000 });

    await page.getByRole('button', { name: 'New Draft', exact: true }).click();

    // The product is picked from its card by name (#162) -- no ids typed.
    await page
      .getByRole('dialog')
      .getByRole('radio', { name: 'GP E2E Product', exact: true })
      .check();

    // Provider, Model, Execution Mode are <select> elements.
    // Mock auto-defaults are applied when the mock capability is available,
    // but ensure they're explicitly set.
    await page.locator('#new-draft-provider').selectOption('mock');
    await page.locator('#new-draft-model').selectOption('mock-v1');
    await page.locator('#new-draft-mode').selectOption('mock');

    // Submit the draft creation form — button text is "Create Draft"
    const createDraftResponse = page.waitForResponse(
      (r) =>
        r.url().includes('/drafts') &&
        r.request().method() === 'POST' &&
        r.status() === 201,
      { timeout: 15_000 }
    );
    await page.getByRole('button', { name: /Create Draft/i }).click();

    const draftBody = (await (await createDraftResponse).json()) as {
      id: string;
      state: string;
    };
    generationDraftId = draftBody.id;
    expect(draftBody.state).toBe('editing');
  });

  await test.step('Preview Generation through UI', async () => {
    // After draft creation, page navigates to draft detail at /generate/drafts/{id}
    await expect(page).toHaveURL(
      new RegExp(`/w/${slug}/generate/drafts/${generationDraftId}`),
      { timeout: 15_000 }
    );

    // Apply template and set variants_per_combination through UI controls
    const templateCheckbox = page.getByLabel('GP E2E Template', {
      exact: true,
    });
    await expect(templateCheckbox).toBeVisible({ timeout: 5_000 });
    const templateUpdateResponse = page.waitForResponse(
      (r) =>
        r.url().includes(`/drafts/${generationDraftId}`) &&
        r.request().method() === 'PATCH' &&
        r.status() === 200,
      { timeout: 15_000 }
    );
    await templateCheckbox.click();
    await templateUpdateResponse;
    await expect(templateCheckbox).toBeChecked();

    const variantInput = page.locator(
      '#format-variant-count, [name="variants_per_combination"]'
    );
    await expect(variantInput).toBeVisible({ timeout: 15_000 });
    // FormatConfig commits typed fields on blur/Enter (one CAS write per
    // edit, not per keystroke), so the PATCH follows the Enter.
    if ((await variantInput.inputValue()) !== '3') {
      const variantUpdateResponse = page.waitForResponse(
        (r) =>
          r.url().includes(`/drafts/${generationDraftId}`) &&
          r.request().method() === 'PATCH' &&
          r.status() === 200,
        { timeout: 15_000 }
      );
      await variantInput.fill('3');
      await variantInput.press('Enter');
      await variantUpdateResponse;
    }

    const previewResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/previews') &&
        r.request().method() === 'POST' &&
        (r.status() === 201 || r.status() === 200),
      { timeout: 15_000 }
    );

    await page.getByRole('button', { name: /Generate Preview/i }).click();

    const previewBody = (await (await previewResponsePromise).json()) as {
      id: string;
      preview_hash: string;
      call_count: number;
    };
    generationPreviewId = previewBody.id;
    expect(previewBody.call_count).toBeGreaterThanOrEqual(1);
  });

  await test.step('Submit Generation run through UI', async () => {
    // Keep the persistent Worker stopped so the recovery boundary below is
    // deterministic instead of racing a sub-second controlled-Mock queue.
    stopWorker();

    const runResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/runs') &&
        r.request().method() === 'POST' &&
        r.status() === 202,
      { timeout: 15_000 }
    );

    await page.getByRole('button', { name: /generate|submit|run/i }).click();

    const runBody = (await (await runResponsePromise).json()) as {
      operation_id: string;
      generation_run_id: string;
    };
    generationRunId = runBody.generation_run_id;
    generationOperationId = runBody.operation_id;
    expect(generationRunId).toBeTruthy();
  });

  await test.step('Wait for at least one RunItem, kill Worker, then recover', async () => {
    const queuedRun = await readJson<{ total: number }>(
      await page.request.get(`${generationBff(slug)}/runs/${generationRunId}`)
    );
    expect(queuedRun.total).toBeGreaterThanOrEqual(2);

    // The Worker prioritizes onboarding outbox delivery before durable Jobs,
    // so the number of one-shot invocations is not the number of RunItems.
    // Drive the official one-shot entry point until every generation dispatch
    // has reached QC, regardless of other queued work.
    let dispatchedItems = 0;
    for (let i = 0; i < queuedRun.total + 20; i += 1) {
      runWorkerOnce();
      const itemPage = await readJson<{
        items: Array<{ state: string }>;
      }>(
        await page.request.get(
          `${generationBff(slug)}/runs/${generationRunId}/items?limit=50`
        )
      );
      dispatchedItems = itemPage.items.filter(
        (item) => item.state !== 'pending' && item.state !== 'queued'
      ).length;
      if (dispatchedItems === queuedRun.total) break;
    }
    expect(dispatchedItems).toBe(queuedRun.total);

    // Then advance exactly until the first QC completion. This leaves the
    // remaining items queued for the persistent Worker recovery below.
    let firstQcDone = false;
    for (let i = 0; i < 20; i += 1) {
      runWorkerOnce();
      const detail = await readJson<{ done: number }>(
        await page.request.get(`${generationBff(slug)}/runs/${generationRunId}`)
      );
      if (detail.done >= 1) {
        firstQcDone = true;
        break;
      }
    }
    expect(firstQcDone).toBe(true);

    // Wait for done >= 1 AND done < total (guarantees mid-run recovery).
    await expect
      .poll(
        async () => {
          const runDetail = await readJson<{
            status: string;
            done: number;
            total: number;
          }>(
            await page.request.get(
              `${generationBff(slug)}/runs/${generationRunId}`
            )
          );
          if (
            runDetail.total >= 2 &&
            runDetail.done >= 1 &&
            runDetail.done < runDetail.total
          ) {
            return 'mid-run';
          }
          if (runDetail.done === runDetail.total && runDetail.total > 0) {
            return 'already-done';
          }
          return 'waiting';
        },
        { timeout: 60_000, intervals: [500] }
      )
      .toBe('mid-run');

    const preKillRun = await readJson<{
      done: number;
      total: number;
      status: string;
    }>(
      await page.request.get(`${generationBff(slug)}/runs/${generationRunId}`)
    );
    expect(preKillRun.done).toBeGreaterThanOrEqual(1);
    expect(preKillRun.done).toBeLessThan(preKillRun.total);

    // The persistent Worker remains stopped at this boundary.
    stopWorker();

    const preKillSnapshot = inspectCanonicalSnapshot({
      workspaceId: ws.workspaceId,
      ownerUserId,
    });

    // Restart worker to recover
    startWorker();

    // Wait for run to complete
    await expect
      .poll(
        async () => {
          const detail = await readJson<{ status: string }>(
            await page.request.get(
              `${generationBff(slug)}/runs/${generationRunId}`
            )
          );
          return detail.status;
        },
        { timeout: 120_000, intervals: [2000] }
      )
      .toMatch(/succeeded|partially_succeeded/);

    // Verify no duplication after recovery
    const postRecoverySnapshot = inspectCanonicalSnapshot({
      workspaceId: ws.workspaceId,
      ownerUserId,
    });
    expect(postRecoverySnapshot.generation!.runCount).toBe(
      preKillSnapshot.generation!.runCount
    );
    expect(postRecoverySnapshot.generation!.costReservationCount).toBe(
      postRecoverySnapshot.generation!.totalItems
    );
    expect(postRecoverySnapshot.generation!.providerCallCount).toBe(
      postRecoverySnapshot.generation!.totalItems
    );
    // Each image is quality-checked by at most one governed call, even when
    // its QC Job is re-run after the Worker restart (the call is replayed).
    expect(
      postRecoverySnapshot.generation!.qualityCheckCallCount ?? 0
    ).toBeLessThanOrEqual(postRecoverySnapshot.generation!.totalItems);
  });

  await test.step('Collect generated asset versions', async () => {
    const assets = await readJson<{
      items: Array<{ id: string; asset_id: string }>;
    }>(await page.request.get(`${generationBff(slug)}/assets`));
    assetVersionIds = assets.items.map((a) => a.id);
    expect(assetVersionIds.length).toBeGreaterThanOrEqual(1);
  });

  // =========================================================================
  // STEP 7: Assets — keyboard review, regeneration, ZIP + Manifest export
  // =========================================================================

  await test.step('Review assets via keyboard shortcuts', async () => {
    await page.goto(assetsUrl(slug));
    await expect(
      page.getByRole('heading', { name: 'Assets', exact: true })
    ).toBeVisible({ timeout: 15_000 });

    // Select assets via checkboxes — SelectionBar (with Review button)
    // only appears when ≥1 asset is selected
    const assetCheckboxes = page.getByTestId('asset-select');
    await expect(assetCheckboxes.first()).toBeVisible({ timeout: 15_000 });
    const checkboxCount = Math.min(await assetCheckboxes.count(), 4);
    expect(checkboxCount).toBe(4);
    for (let i = 0; i < checkboxCount; i++) {
      await assetCheckboxes.nth(i).check({ force: true });
      await expect(assetCheckboxes.nth(i)).toBeChecked();
    }

    // Enter review mode via SelectionBar
    const reviewModeBtn = page.getByTestId('selection-review');
    await expect(reviewModeBtn).toBeVisible({ timeout: 5_000 });
    await reviewModeBtn.click();

    const submitReviewShortcut = async (
      key: 'a' | 'c' | 'x',
      reviewValue: 'approved' | 'needs_changes' | 'rejected',
      expectedCount: number
    ) => {
      await expect
        .poll(
          async () => {
            await page.keyboard.press(key);
            await page.waitForTimeout(250);
            const reviewed = await readJson<{
              items: Array<{ id: string }>;
            }>(
              await page.request.get(
                `${generationBff(slug)}/assets?review_value=${reviewValue}`
              )
            );
            return reviewed.items.length;
          },
          { timeout: 15_000, intervals: [350] }
        )
        .toBe(expectedCount);
    };

    // Approve first asset with 'A' key
    await submitReviewShortcut('a', 'approved', 1);

    // Approve second asset with 'A' key (experiments require ≥2 approved)
    if (assetVersionIds.length > 1) {
      await submitReviewShortcut('a', 'approved', 2);
    }

    // If there are more assets, use other review shortcuts
    if (assetVersionIds.length > 2) {
      await submitReviewShortcut('c', 'needs_changes', 1);
    }
    if (assetVersionIds.length > 3) {
      await submitReviewShortcut('x', 'rejected', 1);
    }

    // Retrieve all approved asset version IDs
    const approvedAssets = await readJson<{
      items: Array<{ id: string }>;
    }>(
      await page.request.get(
        `${generationBff(slug)}/assets?review_value=approved`
      )
    );
    expect(approvedAssets.items.length).toBeGreaterThanOrEqual(2);
    approvedAssetVersionId = approvedAssets.items[0].id;

    // Navigate back with arrow keys
    await page.keyboard.press('ArrowLeft');
    await page.waitForTimeout(300);
    await page.keyboard.press('Escape');
  });

  await test.step('Regenerate one asset through UI and complete it', async () => {
    // Navigate to the asset that needs regeneration
    await page.goto(assetsUrl(slug));
    await expect(
      page.getByRole('heading', { name: 'Assets', exact: true })
    ).toBeVisible({ timeout: 15_000 });

    // Select the asset to regenerate (second asset or first if only one)
    const targetIndex = assetVersionIds.length > 1 ? 1 : 0;
    const assetCard = page.getByTestId('asset-card').nth(targetIndex);
    await expect(assetCard).toBeVisible({ timeout: 15_000 });
    await assetCard.getByRole('button').click();

    // Click regenerate button
    const regenDraftPromise = page.waitForResponse(
      (r) =>
        r.url().includes('/regeneration-drafts') &&
        r.request().method() === 'POST' &&
        (r.status() === 200 || r.status() === 201),
      { timeout: 15_000 }
    );

    // The panel exposes each regeneration intent as a direct command button.
    await page.getByTestId('regenerate-new_seed').click();

    const draft = (await (await regenDraftPromise).json()) as {
      id: string;
    };
    regeneratedDraftId = draft.id;
    expect(regeneratedDraftId).toBeTruthy();

    // Navigate from the Lightbox to the regeneration draft detail page
    await page.goto(`/w/${slug}/generate/drafts/${regeneratedDraftId}`);
    await expect(
      page.getByRole('heading', { name: 'Draft configuration' })
    ).toBeVisible({
      timeout: 15_000,
    });

    const regenPreviewPromise = page.waitForResponse(
      (r) =>
        r.url().includes('/previews') &&
        r.request().method() === 'POST' &&
        (r.status() === 200 || r.status() === 201),
      { timeout: 15_000 }
    );
    await page.getByRole('button', { name: /Generate Preview/i }).click();
    const regenPreview = (await (await regenPreviewPromise).json()) as {
      id: string;
    };
    expect(regenPreview.id).toBeTruthy();

    // Submit the regeneration run from the draft detail page
    const regenRunPromise = page.waitForResponse(
      (r) =>
        r.url().includes('/runs') &&
        r.request().method() === 'POST' &&
        (r.status() === 200 || r.status() === 201 || r.status() === 202),
      { timeout: 15_000 }
    );

    const submitRegenRun = page.getByRole('button', {
      name: /submit.*run|generate/i,
    });
    await expect(submitRegenRun).toBeEnabled({ timeout: 15_000 });
    await submitRegenRun.click();

    const run = (await (await regenRunPromise).json()) as {
      operation_id: string;
      generation_run_id: string;
    };
    expect(run.generation_run_id).toBeTruthy();

    // Wait for regeneration to complete
    await expect
      .poll(
        async () => {
          const detail = await readJson<{ status: string }>(
            await page.request.get(
              `${generationBff(slug)}/runs/${run.generation_run_id}`
            )
          );
          return detail.status;
        },
        { timeout: 60_000, intervals: [2000] }
      )
      .toMatch(/succeeded|partially_succeeded/);
  });

  await test.step('Export ZIP + Manifest through UI', async () => {
    await page.goto(assetsUrl(slug));
    await expect(
      page.getByRole('heading', { name: 'Assets', exact: true })
    ).toBeVisible({ timeout: 15_000 });

    // Select approved assets — SelectionBar (with Export button) requires selection
    const exportCheckboxes = page.getByTestId('asset-select');
    await expect(exportCheckboxes.first()).toBeVisible({ timeout: 15_000 });
    await exportCheckboxes.first().check({ force: true });
    await expect(exportCheckboxes.first()).toBeChecked();

    // Click export button
    const exportResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/exports') &&
        r.request().method() === 'POST' &&
        r.status() === 202,
      { timeout: 15_000 }
    );

    await page.getByTestId('selection-export').click();

    // If there's an export options dialog, select approved_only
    const approvedOnlyOption = page.getByTestId('export-preset-approved_only');
    await expect(approvedOnlyOption).toBeVisible({ timeout: 5_000 });
    await approvedOnlyOption.check();
    await expect(approvedOnlyOption).toBeChecked();

    await page.getByTestId('export-start').click();

    const created = (await (await exportResponsePromise).json()) as {
      package_id: string;
      operation_id: string;
    };
    exportPackageId = created.package_id;

    // Wait for export to be ready (poll via status indicator or API)
    await expect
      .poll(
        async () => {
          const detail = await readJson<{ state: string }>(
            await page.request.get(
              `${generationBff(slug)}/exports/${exportPackageId}`
            )
          );
          return detail.state;
        },
        { timeout: 60_000, intervals: [2000] }
      )
      .toBe('ready');

    // Verify manifest content
    const manifest = await readJson<{
      package_id: string;
      entries: Array<{
        asset_version_id: string;
        content_sha256: string;
        origin_kind: string;
        execution_mode: string;
      }>;
      total_versions: number;
    }>(
      await page.request.get(
        `${generationBff(slug)}/exports/${exportPackageId}/manifest`
      )
    );
    expect(manifest.entries.length).toBeGreaterThanOrEqual(1);
    expect(manifest.entries[0].execution_mode).toBe('mock');

    // Download the ZIP via the UI download button (unconditional — must exist)
    const downloadBtn = page.getByTestId('export-download');
    await expect(downloadBtn).toBeVisible({ timeout: 10_000 });

    const downloadResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/download-intent') &&
        r.request().method() === 'POST' &&
        (r.status() === 200 || r.status() === 201),
      { timeout: 15_000 }
    );
    await downloadBtn.click();

    const download = (await (await downloadResponsePromise).json()) as {
      intent_id: string;
      download_grant: { url: string; kind: string };
    };
    expect(download.download_grant.kind).toBe('signed_get');

    // Fetch ZIP and verify magic bytes
    const zipResponse = await page.request.get(download.download_grant.url);
    expect(zipResponse.ok()).toBe(true);

    const zipBody = await zipResponse.body();
    expect(zipBody.length).toBeGreaterThan(0);

    const zipMagic = zipBody.slice(0, 4).toString('hex');
    expect(zipMagic).toBe('504b0304');
  });

  // =========================================================================
  // STEP 8: Browser refresh — verify state persists
  // =========================================================================

  await test.step('Browser refresh — Library state persists', async () => {
    await page.goto(libraryUrl(slug));
    await page.reload();
    await expect(page.getByText('GP E2E Product')).toBeVisible({
      timeout: 15_000,
    });
  });

  // =========================================================================
  // STEP 9: Experiments — create, bind, freeze, start, judge, confirm Winner
  // =========================================================================

  await test.step('Create Experiment through UI (select 2 approved assets)', async () => {
    // Collect approved asset IDs first (need at least 2)
    const approvedList = await readJson<{
      items: Array<{ id: string; product_id: string }>;
    }>(
      await page.request.get(
        `${generationBff(slug)}/assets?review_value=approved`
      )
    );
    expect(approvedList.items.length).toBeGreaterThanOrEqual(2);
    const experimentAssetIds = approvedList.items.slice(0, 2).map((a) => a.id);

    await page.goto(experimentsUrl(slug));
    await expect(
      page.getByRole('heading', { name: 'Experiments', exact: true })
    ).toBeVisible({
      timeout: 15_000,
    });

    await page.getByRole('button', { name: /New experiment/i }).click();

    // Fill experiment name
    await page.getByPlaceholder(/creative test/i).fill('GP E2E Experiment');

    // Select approved assets via checkbox grid (at least 2 required)
    for (const avId of experimentAssetIds) {
      const checkbox = page.locator(
        `input[type="checkbox"][value="${avId}"], ` +
          `[data-asset-version-id="${avId}"] input[type="checkbox"]`
      );
      if (await checkbox.isVisible({ timeout: 5_000 })) {
        await checkbox.check();
      } else {
        // Fall back to clicking the row containing the asset ID prefix
        const row = page.getByText(new RegExp(avId.slice(0, 8)));
        await row.click();
      }
    }

    // Fill decision context fields (text inputs, not selects)
    await page.getByLabel(/Product version/i).fill(productVersionId);
    await page.getByLabel(/Offer version/i).fill(offerVersionId);
    await page.getByLabel(/Market/i).fill('US');
    await page.getByLabel(/Currency/i).fill('USD');

    // Placement is a select
    const placementSelect = page.getByLabel(/Placement/i);
    if (await placementSelect.isVisible({ timeout: 3_000 })) {
      await placementSelect.selectOption('feed');
    }

    // Budget fields
    await page.getByLabel(/Daily budget/i).fill('50.00');
    await page.getByLabel(/Total budget/i).fill('400.00');

    // Schedule
    // The offer published earlier becomes valid in seven days. Keep the
    // experiment schedule inside that immutable offer-validity window.
    const experimentStart = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000);
    const experimentEnd = new Date(Date.now() + 14 * 24 * 60 * 60 * 1000);
    const startInput = page.getByLabel(/Start date/i);
    if (await startInput.isVisible({ timeout: 3_000 })) {
      await startInput.fill(experimentStart.toISOString().slice(0, 10));
    }
    const endInput = page.getByLabel(/End date/i);
    if (await endInput.isVisible({ timeout: 3_000 })) {
      await endInput.fill(experimentEnd.toISOString().slice(0, 10));
    }

    // Submit — "Create draft" button. This creates the experiment and
    // adds entries for each checked asset version in one action.
    const createResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/experiments') &&
        r.request().method() === 'POST' &&
        r.status() === 201,
      { timeout: 15_000 }
    );
    await page.getByRole('button', { name: /Create draft/i }).click();

    const expBody = (await (await createResponsePromise).json()) as {
      id: string;
      state: string;
    };
    experimentId = expBody.id;
    expect(expBody.state).toBe('draft');

    // Creating an experiment is a multi-request UI workflow: the draft is
    // created first, then each selected asset is appended as an entry. Wait
    // for the shallow route change that happens only after those commands
    // finish so the next step cannot cancel the in-flight entry requests.
    await page.waitForURL(
      (url) => url.searchParams.get('experiment') === experimentId,
      { timeout: 15_000 }
    );
    await expect
      .poll(
        async () => {
          const response = await page.request.get(
            `/api/domain/v1/w/${slug}/experiments/${experimentId}`
          );
          if (!response.ok()) return -1;
          const body = (await response.json()) as { entries?: unknown[] };
          return body.entries?.length ?? 0;
        },
        { timeout: 15_000 }
      )
      .toBe(experimentAssetIds.length);
  });

  await test.step('Freeze experiment through UI', async () => {
    // Navigate to the experiment detail using ?experiment= URL pattern.
    //
    // The create step leaves the page on this same experiment, so the page
    // has usually fetched duplicate-analysis already. A browser
    // waitForResponse registered before goto() can resolve on that previous
    // document's response, whose body Chromium discards once goto() commits
    // ("No resource with given identifier found"). Read the analysis through
    // the page's request context instead: it is the same deterministic
    // server analysis, has a body that never belongs to a document, and
    // cannot be matched against the wrong navigation.
    await page.goto(`${experimentsUrl(slug)}?experiment=${experimentId}`);
    await expect(
      page.getByRole('heading', { name: 'GP E2E Experiment', exact: true })
    ).toBeVisible({
      timeout: 15_000,
    });
    const duplicateAnalysis = await readJson<{
      has_duplicates: boolean;
      duplicate_groups?: unknown[];
    }>(
      await page.request.get(
        `/api/domain/v1/w/${slug}/experiments/${experimentId}/duplicate-analysis`
      )
    );

    // The generated matrix can legitimately contain candidates that share a
    // concept dimension. Resolve every reported group through the product UI
    // before acknowledging the immutable freeze context.
    if (duplicateAnalysis.has_duplicates) {
      const duplicateGroupCount =
        duplicateAnalysis.duplicate_groups?.length ?? 0;
      await expect(
        page.getByRole('heading', { name: 'Duplicate Concept Resolution' })
      ).toBeVisible();
      const allowDuplicateRadios = page.getByLabel(
        'Allow (controlled duplicate)'
      );
      const resolutionReasons = page.getByPlaceholder(
        'Explain this resolution decision…'
      );
      await expect(allowDuplicateRadios).toHaveCount(duplicateGroupCount);
      await expect(resolutionReasons).toHaveCount(duplicateGroupCount);
      for (let index = 0; index < duplicateGroupCount; index += 1) {
        await allowDuplicateRadios.nth(index).check();
        await resolutionReasons
          .nth(index)
          .fill('Golden path controlled-duplicate coverage.');
      }
    }

    // Check the acknowledgement checkbox required before freeze. The page
    // clears it once, when its own duplicate-analysis result first arrives
    // (a stale plan must never be frozen). The analysis above was read
    // outside the page, so the tick can race that reset; re-tick until the
    // Freeze button settles enabled instead of depending on response order.
    const acknowledgement = page.getByLabel(
      /I reviewed the candidates, budget, schedule, and immutable context/i
    );
    const freezeButton = page.getByRole('button', {
      name: /Freeze experiment/i,
    });
    await expect(async () => {
      if (!(await acknowledgement.isChecked())) await acknowledgement.check();
      await expect(freezeButton).toBeEnabled({ timeout: 3_000 });
    }).toPass({ timeout: 30_000, intervals: [500, 1_000, 2_000] });

    const freezeResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/freeze') &&
        r.request().method() === 'POST' &&
        (r.status() === 200 || r.status() === 201),
      { timeout: 15_000 }
    );

    await freezeButton.click();

    const frozenBody = (await (await freezeResponsePromise).json()) as {
      state: string;
      version_id: string;
      candidates: Array<{ id: string }>;
    };
    expect(frozenBody.state).toBe('frozen');
    experimentVersionId = frozenBody.version_id;
    candidateIds = frozenBody.candidates.map((c) => c.id);
  });

  await test.step('Bind candidates through UI', async () => {
    for (let i = 0; i < candidateIds.length; i++) {
      const position = i + 1;
      const candidateCard = page.getByRole('group', {
        name: `Candidate ${position}`,
      });

      // Fill ad account and ad ID using exact label patterns from the UI
      await candidateCard
        .getByLabel(`Ad account for candidate ${position}`)
        .fill(`act_gp_exp_${experimentId.slice(0, 8)}`);

      await candidateCard
        .getByLabel(`Ad ID for candidate ${position}`)
        .fill(`ad_${candidateIds[i].slice(0, 16)}`);

      const bindResponsePromise = page.waitForResponse(
        (response) =>
          response.url().includes(`/candidates/${candidateIds[i]}/bind`) &&
          response.request().method() === 'POST' &&
          response.status() === 200,
        { timeout: 15_000 }
      );
      const bindBtn = candidateCard.getByRole('button', { name: /^Bind$/i });
      await bindBtn.click();
      await bindResponsePromise;
      await expect(
        candidateCard.getByText('Bound', { exact: true })
      ).toBeVisible({
        timeout: 15_000,
      });
    }
  });

  await test.step('Browser refresh — experiment frozen state persists', async () => {
    await page.reload();
    await expect(page.getByText(/^frozen$/i).first()).toBeVisible({
      timeout: 15_000,
    });
  });

  // =========================================================================
  // STEP 9b: Sandbox Launch (PAUSED) — must happen in frozen state
  // =========================================================================

  await test.step('Create PAUSED Sandbox launch through UI', async () => {
    // Launch controls only visible in frozen state (before Start measurement)
    await page.goto(`${experimentsUrl(slug)}?experiment=${experimentId}`);
    await expect(
      page.getByRole('heading', { name: 'GP E2E Experiment', exact: true })
    ).toBeVisible({
      timeout: 15_000,
    });

    // Fill required connection and account fields before creating preview
    await page
      .getByLabel(/Sandbox Meta connection ID/i)
      .fill(infraResult.customerSandboxConnectionId);
    await page
      .getByLabel(/Sandbox Meta ad account ID/i)
      .fill(infraResult.customerSandboxAdAccountId);

    // The panel pre-fills the experiment's daily budget (50.00 -> 5000 minor
    // units), but the Beta launch guard rejects anything above
    // LAUNCH_MAX_DAILY_BUDGET_MINOR (500 by default) with launch_daily_budget_invalid.
    // Stay inside the product cap instead of loosening it for the scenario.
    await page.getByLabel(/Daily budget per candidate/i).fill('500');

    // Step 1: "Create Sandbox preview" — creates the preview
    const previewResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/previews') &&
        r.request().method() === 'POST' &&
        (r.status() === 200 || r.status() === 201),
      { timeout: 15_000 }
    );

    await page.getByRole('button', { name: /Create Sandbox preview/i }).click();

    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const preview = (await (await previewResponsePromise).json()) as {
      id: string;
      preview_hash: string;
    };

    // Step 2: Check launch acknowledgement, then "Confirm Sandbox preview"
    await page.getByLabel(/I reviewed every exact candidate/i).check();

    const confirmResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/confirm') &&
        r.request().method() === 'POST' &&
        (r.status() === 200 || r.status() === 201),
      { timeout: 15_000 }
    );

    await page
      .getByRole('button', { name: /Confirm Sandbox preview/i })
      .click();

    await confirmResponsePromise;

    // Step 3: Check execute acknowledgement, then "Execute PAUSED Sandbox launch"
    await page.getByLabel(/Execute this exact/i).check();

    // execute_meta_launch_preview answers 202 Accepted: the PAUSED saga runs
    // on the Worker (contracts/openapi/marketai-domain-v1.json).
    const executeResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/execute') &&
        r.request().method() === 'POST' &&
        r.status() === 202,
      { timeout: 15_000 }
    );

    await page
      .getByRole('button', { name: /Execute PAUSED Sandbox launch/i })
      .click();

    const executed = (await (await executeResponsePromise).json()) as {
      id: string;
      operation_id: string;
    };

    // Wait for the Worker saga to finish. get_meta_launch_provisioning is
    // scoped by meta_mode, so the status read names the mode it launched in.
    await expect
      .poll(
        async () => {
          const status = await readJson<{
            provisioning_state: string;
          }>(
            await page.request.get(
              `${launchBff(slug)}/operations/${executed.id}?meta_mode=customer_sandbox`
            )
          );
          return status.provisioning_state;
        },
        { timeout: 60_000, intervals: [2000] }
      )
      .toBe('provisioned');

    // The provisioning id lives in the page URL, so a reload must render the
    // same terminal state through the UI.
    await page.reload();
    await expect(page.locator('[data-provisioning-state]')).toHaveText(
      'provisioned',
      { timeout: 15_000 }
    );

    // Verify PAUSED fake objects exist
    const fakeObjects = inspectFakeMetaObjects(
      ws.workspaceId,
      'customer_sandbox',
      infraResult.customerSandboxScenarioVersionId,
      ownerUserId
    );
    expect(fakeObjects.length).toBeGreaterThanOrEqual(1);
    for (const obj of fakeObjects) {
      if (obj.state && typeof obj.state === 'object') {
        const status =
          (obj.state as Record<string, unknown>).effective_status ??
          (obj.state as Record<string, unknown>).status;
        if (status) {
          expect(status).toBe('PAUSED');
        }
      }
    }
  });

  await test.step('Start experiment through UI', async () => {
    const startResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/start') &&
        r.request().method() === 'POST' &&
        (r.status() === 200 || r.status() === 201),
      { timeout: 15_000 }
    );

    await page.getByRole('button', { name: /Start measurement/i }).click();

    const startedBody = (await (await startResponsePromise).json()) as {
      state: string;
    };
    expect(startedBody.state).toBe('running');
  });

  await test.step('Seed controlled metrics via sync/projection path', async () => {
    // The Sandbox Launch stays PAUSED, so the first observation windows carry
    // no delivery. judge-v1 compares each candidate with the frozen absolute
    // CTR threshold; without impressions there is no evidence to compare, so
    // every candidate must be judged `inconclusive` (reason `no_impressions`).
    seedExperimentMetrics(ws.workspaceId, experimentId, ownerUserId, {
      days: 3,
      candidateMetrics: candidateIds.map((cId) => ({
        candidateId: cId,
        dailyImpressions: 0,
        dailyClicks: 0,
        dailyPurchases: 0,
        dailySpend: '0.00',
      })),
    });
  });

  await test.step('Judge experiment through UI — first inconclusive, then decisive', async () => {
    type JudgementResponse = {
      judgement: {
        result: {
          entries: Array<{
            entry_id: string;
            verdict: string;
            reason_codes: string[];
          }>;
          summary: Record<string, number>;
        };
      };
      proposal_id: string | null;
      proposal_state: string | null;
    };

    // Navigate to the experiment detail using ?experiment= URL pattern
    await page.goto(`${experimentsUrl(slug)}?experiment=${experimentId}`);
    await expect(
      page.getByRole('heading', { name: 'GP E2E Experiment', exact: true })
    ).toBeVisible({
      timeout: 15_000,
    });

    // Click "Run judgement" button
    const judgeResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/judgements') &&
        r.request().method() === 'POST' &&
        (r.status() === 200 || r.status() === 201),
      { timeout: 15_000 }
    );

    await page.getByRole('button', { name: /Run judgement/i }).click();

    const j1 = (await (await judgeResponsePromise).json()) as JudgementResponse;

    // First judgement: zero delivery is insufficient evidence, so no
    // candidate receives a material verdict.
    expect(j1.judgement.result.summary).toEqual({
      win: 0,
      learn: 0,
      lose: 0,
      inconclusive: candidateIds.length,
    });
    for (const entry of j1.judgement.result.entries) {
      expect(entry.verdict).toBe('inconclusive');
      expect(entry.reason_codes).toContain('no_impressions');
    }

    // Every judgement records a proposal that preserves its statistical
    // output, but an all-inconclusive proposal is not actionable: the Domain
    // rejects its confirmation (no_material_verdicts) and the UI offers no
    // Winner confirmation for it.
    expect(j1.proposal_id).toBeTruthy();
    const inconclusiveProposal = page.locator(
      `[data-proposal-id="${j1.proposal_id}"]`
    );
    await expect(inconclusiveProposal.getByRole('status')).toContainText(
      /Inconclusive evidence cannot be confirmed as a Winner/i,
      { timeout: 15_000 }
    );
    await expect(
      inconclusiveProposal.getByRole('button', { name: 'No Winner to confirm' })
    ).toBeDisabled();
    await expect(
      page.getByRole('button', { name: 'Confirm proposal' })
    ).toHaveCount(0);

    {
      // Seed divergent metrics starting from day 4 to avoid overlapping
      // the first 3-day sync (uq_ad_metric_contract_day constraint)
      seedExperimentMetrics(ws.workspaceId, experimentId, ownerUserId, {
        days: 14,
        dayOffset: 3,
        candidateMetrics: candidateIds.map((cId, i) => ({
          candidateId: cId,
          dailyImpressions: i === 0 ? 5000 : 800,
          dailyClicks: i === 0 ? 500 : 40,
          dailyPurchases: i === 0 ? 50 : 5,
          dailySpend: '50.00',
        })),
      });

      // Reload experiment page and re-judge through UI
      await page.goto(`${experimentsUrl(slug)}?experiment=${experimentId}`);
      await expect(
        page.getByRole('heading', { name: 'GP E2E Experiment', exact: true })
      ).toBeVisible({
        timeout: 15_000,
      });

      const j2ResponsePromise = page.waitForResponse(
        (r) =>
          r.url().includes('/judgements') &&
          r.request().method() === 'POST' &&
          (r.status() === 200 || r.status() === 201),
        { timeout: 15_000 }
      );

      await page.getByRole('button', { name: /Run judgement/i }).click();

      const j2 = (await (await j2ResponsePromise).json()) as JudgementResponse;

      expect(j2.judgement.result.summary.win).toBeGreaterThanOrEqual(1);
      expect(j2.proposal_id).toBeTruthy();
      expect(j2.proposal_state).toBe('created');

      // Acknowledge the evidence on the decisive proposal before confirming.
      const decisiveProposal = page.locator(
        `[data-proposal-id="${j2.proposal_id}"]`
      );
      await decisiveProposal
        .getByLabel(
          /I reviewed the evidence and understand this proposal does not become a Winner until I confirm it/i
        )
        .check();

      const confirmResponsePromise = page.waitForResponse(
        (r) =>
          r.url().includes('/confirm') &&
          r.request().method() === 'POST' &&
          (r.status() === 200 || r.status() === 201),
        { timeout: 15_000 }
      );

      await decisiveProposal
        .getByRole('button', { name: 'Confirm proposal' })
        .click();

      const confirmed = (await (await confirmResponsePromise).json()) as {
        decisions: Array<{
          id: string;
          verdict: string;
          candidate_id: string;
        }>;
      };

      const winnerDecision = confirmed.decisions.find(
        (d) => d.verdict === 'win' || d.verdict === 'winner'
      );
      expect(winnerDecision).toBeTruthy();
      decisionId = winnerDecision!.id;

      const winnerCandidate = candidateIds.indexOf(
        winnerDecision!.candidate_id
      );
      winnerAssetVersionId =
        assetVersionIds[winnerCandidate] ?? assetVersionIds[0];
    }
  });

  // =========================================================================
  // STEP 10: Localization
  // =========================================================================

  await test.step('Ensure winner asset is approved for Localization', async () => {
    // The winner asset should already be approved from the keyboard review step.
    // Navigate to assets and verify the approval badge is visible.
    await page.goto(assetsUrl(slug));
    await expect(
      page.getByRole('heading', { name: 'Assets', exact: true })
    ).toBeVisible({ timeout: 15_000 });

    const approvedAssets = await readJson<{
      items: Array<{ id: string }>;
    }>(
      await page.request.get(
        `${generationBff(slug)}/assets?review_value=approved`
      )
    );
    const winnerIsApproved = approvedAssets.items.some(
      (a) => a.id === winnerAssetVersionId
    );

    if (!winnerIsApproved) {
      // Navigate to the winner asset and approve it through the UI
      const winnerCard = page.locator(
        `[data-asset-version-id="${winnerAssetVersionId}"]`
      );
      if (await winnerCard.isVisible({ timeout: 5_000 })) {
        await winnerCard.click();
      }

      await page.getByTestId('review-approve').click();

      const approveConfirm = page.getByRole('button', {
        name: /confirm.*approve/i,
      });
      if (await approveConfirm.isVisible({ timeout: 3_000 })) {
        await approveConfirm.click();
      }

      await page.waitForTimeout(1000);
    }
  });

  let locInfra: ReturnType<typeof seedLocalizationInfrastructure>;

  await test.step('Seed Localization platform prerequisites', async () => {
    locInfra = seedLocalizationInfrastructure(
      ws.workspaceId,
      winnerAssetVersionId,
      ownerUserId
    );
    // GB (ready), CA (locked-copy QC failure), AU (unavailable), plus the
    // Winner's own experiment market: Scale enrollment requires the
    // MarketAssetBinding this batch publishes for exactly that market.
    expect(locInfra.markets.length).toBe(4);
    expect(
      locInfra.markets.filter((m) => m.isWinnerMarket).map((m) => m.market)
    ).toEqual(['US']);
    expect(locInfra.truthPolicyVersionId).toBeTruthy();
    expect(locInfra.dataSourceVersionId).toBeTruthy();
  });

  await test.step('Create Localization batch through UI', async () => {
    // Include ALL markets — don't filter out unavailable ones.
    // The unavailable market exercises the skip/fail path.
    const allMarkets = locInfra.markets.map((m) => m.market);

    batchId = await createLocalizationBatchThroughUi(
      page,
      slug,
      winnerAssetVersionId,
      allMarkets
    );
    expect(batchId).toMatch(/^lb_/);
  });

  await test.step('Prepare Localization batch (truth + claims)', async () => {
    await prepareLocalizationBatchThroughUi(
      page,
      slug,
      batchId,
      locInfra.truthPolicyVersionId,
      locInfra.inventoryId,
      locInfra.markets.length
    );
  });

  await test.step('Confirm Localization batch and wait for items', async () => {
    const confirmed = await confirmLocalizationBatchThroughUi(
      page,
      slug,
      batchId
    );
    expect(confirmed.item_ids.length).toBeGreaterThanOrEqual(1);

    // Wait for items to settle
    await expect
      .poll(
        async () => {
          const snapshot = inspectCanonicalSnapshot({
            workspaceId: ws.workspaceId,
            ownerUserId,
            batchId,
          });
          if (!snapshot.localization) return 'missing';
          const states = snapshot.localization.items?.map((i) => i.state) ?? [];
          const terminal = states.every(
            (s) =>
              s === 'succeeded' ||
              s === 'failed' ||
              s === 'skipped' ||
              s === 'retry_wait'
          );
          return terminal ? 'settled' : 'running';
        },
        { timeout: 120_000, intervals: [3000] }
      )
      .toBe('settled');
  });

  await test.step('Retry failed/retry_wait Localization items through UI', async () => {
    const snapshot = inspectCanonicalSnapshot({
      workspaceId: ws.workspaceId,
      ownerUserId,
      batchId,
    });
    // The Domain allows an item-level retry only from retry_wait with
    // attempts left (app/localization/queries.py::_item_actions).
    const retryItem = snapshot.localization?.items?.find(
      (i) => i.state === 'retry_wait'
    );
    expect(retryItem, 'a retry_wait Localization item').toBeTruthy();

    // The control room persists its batch/item selection as ?batch=&item=;
    // the item matrix cells carry no identifiers, so open the item there.
    await page.goto(
      `${localizationUrl(slug)}?batch=${batchId}&item=${retryItem!.id}`
    );
    await expect(
      page.getByRole('heading', { name: 'Localization control room' })
    ).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(`· ${retryItem!.id}`)).toBeVisible({
      timeout: 15_000,
    });

    const retryItemUrl = `${localizationBff(slug)}/batches/${batchId}/items/${retryItem!.id}`;
    const itemAttempt = async () => {
      const item = await readJson<{ state: string; attempt_count: number }>(
        await page.request.get(retryItemUrl)
      );
      return `${item.state}:${item.attempt_count}`;
    };
    expect(await itemAttempt()).toBe('retry_wait:1');

    // The locked-copy QC failure (forbidden term) fails every attempt, so,
    // as in web/e2e/localization/partial-retry.spec.ts, retry to the
    // attempt bound: retry_wait:2, then failed:3. Every item is then
    // terminal before the canonical pre/post-restart snapshots.
    const retryButton = page.getByRole('button', { name: 'Retry item' });
    for (const expected of ['retry_wait:2', 'failed:3']) {
      await expect(retryButton).toBeEnabled({ timeout: 15_000 });
      // Retry executes directly -- the inspector has no confirmation dialog.
      const retryResponsePromise = page.waitForResponse(
        (r) =>
          r.url().includes('/retry') &&
          r.request().method() === 'POST' &&
          (r.status() === 200 || r.status() === 201),
        { timeout: 15_000 }
      );
      await retryButton.click();
      await retryResponsePromise;
      await expect
        .poll(itemAttempt, {
          timeout: 90_000,
          intervals: [250, 500, 1000, 2000, 5000],
        })
        .toBe(expected);
      await page.reload();
    }
    await expect(retryButton).toBeDisabled({ timeout: 15_000 });

    await expect
      .poll(
        async () => {
          const retrySnapshot = inspectCanonicalSnapshot({
            workspaceId: ws.workspaceId,
            ownerUserId,
            batchId,
          });
          const states =
            retrySnapshot.localization?.items?.map((i) => i.state) ?? [];
          return states.every(
            (s) => s === 'succeeded' || s === 'failed' || s === 'skipped'
          );
        },
        { timeout: 60_000, intervals: [3000] }
      )
      .toBe(true);

    // The Winner-market item publishes the MarketAssetBinding Scale needs.
    const winnerMarket = locInfra.markets.find((m) => m.isWinnerMarket)!.market;
    const settled = inspectCanonicalSnapshot({
      workspaceId: ws.workspaceId,
      ownerUserId,
      batchId,
    });
    expect(
      settled.localization?.items?.find((i) => i.targetMarket === winnerMarket)
        ?.state
    ).toBe('succeeded');
  });

  await test.step('Approve the Winner-market localized asset in Assets', async () => {
    // The binding the Winner-market item published waits for review; the
    // exact-version approval in Asset Detail makes it experiment-ready
    // (#128), which the Scale lineage gate requires. No seed approves it.
    const winnerMarket = locInfra.markets.find((m) => m.isWinnerMarket)!.market;
    const approved = await approveLocalizedAssetVersionThroughAssets(
      page,
      slug,
      batchId,
      winnerMarket
    );
    expect(approved.localizedAssetVersionId).not.toBe(winnerAssetVersionId);
  });

  // =========================================================================
  // STEP 12: Scale — enroll, activate, evidence, evaluate, advice
  // =========================================================================

  let scaleInfra: ReturnType<typeof seedScaleInfrastructure>;

  await test.step('Seed Scale infrastructure', async () => {
    scaleInfra = seedScaleInfrastructure(
      ws.workspaceId,
      decisionId,
      ownerUserId
    );
    expect(scaleInfra.deliveryTargetId).toBeTruthy();
    expect(scaleInfra.policyVersionId).toBeTruthy();
    expect(scaleInfra.marketCode).toBe('US');
  });

  await test.step('Enroll in Scale through UI', async () => {
    await page.goto(scaleUrl(slug));
    await expect(
      page.getByRole('heading', { name: 'Scale', exact: true })
    ).toBeVisible({ timeout: 15_000 });

    // Open the CandidateSelector dialog. With no enrollments yet the list's
    // empty state repeats the header's "New Enrollment" action
    // (EnrollmentList.tsx); both open the same selector, use the header's.
    await page
      .getByRole('button', { name: 'New Enrollment', exact: true })
      .first()
      .click();

    // Step 1: Select the Winner Decision from the server-populated picklist.
    // Each option is a button labelled with the full decision id.
    const decisionOption = page.getByRole('button', { name: decisionId });
    await expect(decisionOption).toBeVisible({ timeout: 10_000 });
    await decisionOption.click();

    // Step 2: Enter delivery_target_id — this is a plain text input, not a select
    const deliveryTargetInput = page.getByLabel(/delivery.*target/i);
    await expect(deliveryTargetInput).toBeVisible({ timeout: 5_000 });
    await deliveryTargetInput.fill(scaleInfra.deliveryTargetId);

    // Policy version is server-computed, not a UI input

    // Click "Create" to submit enrollment via candidate-gated path
    const enrollResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/enrollments') &&
        r.request().method() === 'POST' &&
        (r.status() === 200 || r.status() === 201),
      { timeout: 15_000 }
    );

    await page.getByRole('button', { name: /^Create$/i }).click();

    const enrollment = (await (await enrollResponsePromise).json()) as {
      id: string;
      lifecycle: string;
    };
    enrollmentId = enrollment.id;
    expect(enrollment.lifecycle).toBe('draft');
  });

  await test.step('Activate Scale enrollment through UI', async () => {
    // Navigate directly to enrollment detail page
    await page.goto(scaleEnrollmentUrl(slug, enrollmentId));
    await expect(
      page.getByRole('heading', { name: enrollmentId, exact: true })
    ).toBeVisible({
      timeout: 10_000,
    });

    // Open the activation dialog
    await page.getByRole('button', { name: 'Activate', exact: true }).click();
    const activateDialog = page.getByRole('dialog', {
      name: 'Activate enrollment',
    });
    await expect(activateDialog).toBeVisible({ timeout: 10_000 });

    // Step 1: Create activation preview (POST → 201)
    const previewResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/activation-previews') &&
        r.request().method() === 'POST' &&
        r.status() === 201,
      { timeout: 15_000 }
    );

    await activateDialog
      .getByRole('button', { name: 'Create preview' })
      .click();

    const preview = (await (await previewResponsePromise).json()) as {
      id: string;
    };
    expect(preview.id).toBeTruthy();

    // Step 2: Confirm activation with preview ID (PUT → 200)
    const activateResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes('/activate') &&
        r.request().method() === 'PUT' &&
        r.status() === 200,
      { timeout: 15_000 }
    );

    await activateDialog
      .getByRole('button', { name: 'Activate', exact: true })
      .click();

    const activated = (await (await activateResponsePromise).json()) as {
      lifecycle: string;
      current_episode_id: string;
    };
    expect(activated.lifecycle).toBe('active');
    episodeId = activated.current_episode_id;
  });

  await test.step('Advance Scale evidence and evaluate', async () => {
    // customer_sandbox: Scale decisions are ineligible under controlled_mock
    // (ADR 0009). The Beta default Policy the migrations published (#127)
    // needs 14 complete days before evidence is economics_ready. Only
    // confirmed creative fatigue opens
    // refresh_creative -- the one Advice that converts to a GenerationDraft
    // (app/scale/advice.py::convert_occurrence_to_draft) -- and it needs,
    // on consecutive evaluations, high and growing frequency, a supported
    // CTR decline and worsening CPA/ROAS (app/scale/evaluator.py
    // ::_classify_health). So deliver a healthy creative for 14 days, then a
    // fatigued one against that baseline until the Advice opens; once every
    // current-window day is fatigued (day 21) the second consecutive
    // composite hit lands by day 22 at the latest.
    const healthy = {
      spend: '30',
      impressions: 3000,
      clicks: 150,
      purchases: 5,
      purchase_value: '200',
      reach: 1500,
      frequency: '1.4',
    };
    const fatigued = {
      spend: '30',
      impressions: 6000,
      clicks: 60,
      purchases: 1,
      purchase_value: '40',
      reach: 1200,
      frequency: '5.0',
    };
    const epochRows = (await domainDb.$queryRaw`
      SELECT authorization_epoch FROM workspaces WHERE id = ${ws.workspaceId}
    `) as Array<{ authorization_epoch: number | bigint }>;
    const authorizationEpoch = Number(epochRows[0].authorization_epoch);

    let advanced: ReturnType<typeof advanceManifest> | undefined;
    for (let day = 1; day <= 24; day++) {
      advanced = advanceManifest({
        workspaceId: ws.workspaceId,
        workspaceSlug: slug,
        ownerUserId,
        authorizationEpoch,
        metaMode: 'customer_sandbox',
        enrollmentId,
        episodeId,
        policyVersionId: scaleInfra.policyVersionId,
        dayIndex: day,
        kind: 'complete',
        metrics: day <= 14 ? healthy : fatigued,
        baselineMetrics: healthy,
      });
      if (day === 14) {
        expect(advanced.maturity).toBe('economics_ready');
        expect(advanced.health).toBe('healthy');
      }
      if (advanced.adviceThreadIds.length > 0) break;
    }
    expect(advanced?.health).toBe('creative_fatigue');
    expect(advanced?.adviceThreadIds.length).toBeGreaterThanOrEqual(1);

    // Verify evaluations happened
    const snapshot = inspectCanonicalSnapshot({
      workspaceId: ws.workspaceId,
      ownerUserId,
      enrollmentId,
      episodeId,
      metaMode: 'customer_sandbox',
    });
    expect(snapshot.scale?.evaluationCount).toBeGreaterThanOrEqual(15);
  });

  await test.step('Verify Scale advice threads', async () => {
    const snapshot = inspectCanonicalSnapshot({
      workspaceId: ws.workspaceId,
      ownerUserId,
      enrollmentId,
      episodeId,
      metaMode: 'customer_sandbox',
    });
    expect(snapshot.scale?.adviceThreads?.length).toBeGreaterThanOrEqual(1);
    expect(
      snapshot.scale?.adviceThreads?.map((thread) => thread.adviceType)
    ).toContain('refresh_creative');
  });

  let scaleDraftId: string;

  await test.step('Convert Scale advice to GenerationDraft through UI', async () => {
    // Advice threads are listed on the enrollment detail page.
    await page.goto(scaleEnrollmentUrl(slug, enrollmentId));
    await expect(
      page.getByRole('heading', { name: enrollmentId, exact: true })
    ).toBeVisible({ timeout: 15_000 });
    await expect(
      page.getByRole('heading', { name: 'Advice', exact: true })
    ).toBeVisible({ timeout: 15_000 });

    // Click "Convert" on the first advice card — converts advice to a draft
    const draftResponsePromise = page.waitForResponse(
      (r) =>
        (r.url().includes('/convert') || r.url().includes('/generate-draft')) &&
        r.request().method() === 'POST' &&
        (r.status() === 200 || r.status() === 201),
      { timeout: 15_000 }
    );

    await page
      .getByRole('button', { name: /^Convert$/i })
      .first()
      .click();

    const draft = (await (await draftResponsePromise).json()) as {
      generation_draft_id?: string;
      draft_id?: string;
    };
    scaleDraftId = draft.generation_draft_id ?? draft.draft_id ?? '';
    expect(scaleDraftId).toBeTruthy();
  });

  // =========================================================================
  // STEP 13: Take canonical pre-restart snapshot
  // =========================================================================

  let preRestartSnapshot: CanonicalSnapshot;

  await test.step('Capture canonical pre-restart snapshot', async () => {
    preRestartSnapshot = inspectCanonicalSnapshot({
      workspaceId: ws.workspaceId,
      ownerUserId,
      decisionId,
      batchId,
      enrollmentId,
      episodeId,
      metaMode: 'customer_sandbox',
    });

    // Verify all six areas present
    expect(preRestartSnapshot.library).toBeTruthy();
    expect(preRestartSnapshot.generation).toBeTruthy();
    expect(preRestartSnapshot.assets).toBeTruthy();
    expect(preRestartSnapshot.winner?.found).toBe(true);
    expect(preRestartSnapshot.localization?.found).toBe(true);
    expect(preRestartSnapshot.scale?.found).toBe(true);

    // Verify zero Meta writes
    expect(preRestartSnapshot.zeroMetaWrites.offending).toEqual([]);

    // Verify disabled write config
    expect(preRestartSnapshot.metaWriteConfig.metaWriteEnabled).toBe(false);
    expect(preRestartSnapshot.metaWriteConfig.metaWriteAccessToken).toBe('');

    // Verify cross-area ID consistency
    if (preRestartSnapshot.crossAreaIds) {
      expect(preRestartSnapshot.crossAreaIds.idsConsistent).toBe(true);
    }

    // Verify mock provenance
    for (const run of preRestartSnapshot.generation?.mockProvenance ?? []) {
      expect(run.executionMode).toBe('mock');
      expect(run.provider).toBe('mock');
    }

    // Verify NetworkSpy (browser-only evidence)
    expect(spy.offendingRequests).toEqual([]);
  });

  // =========================================================================
  // STEP 14: Full API/Worker/Web restart
  // =========================================================================

  await test.step('Restart API server', async () => {
    await restartApiServer();
  });

  await test.step('Restart Worker', async () => {
    restartWorker();
  });

  await test.step('Restart Web service', async () => {
    await restartWebService();
  });

  // =========================================================================
  // STEP 15: Post-restart verification
  // =========================================================================

  await test.step('Verify all six area pages load after restart', async () => {
    // Area names also label the sidebar links (WorkspaceNavigation), so
    // each page is identified by its own heading, as earlier steps do --
    // a bare text match resolves to several elements.
    await page.goto(`/w/${slug}`);
    await expect(page.locator('#workspace-selector')).toHaveValue(slug, {
      timeout: 30_000,
    });

    await page.goto(libraryUrl(slug));
    await expect(page.getByText('GP E2E Product')).toBeVisible({
      timeout: 15_000,
    });

    await page.goto(generateUrl(slug));
    await expect(
      page.getByRole('heading', { name: 'New Generation', exact: true })
    ).toBeVisible({
      timeout: 15_000,
    });

    await page.goto(assetsUrl(slug));
    await expect(
      page.getByRole('heading', { name: 'Assets', exact: true })
    ).toBeVisible({ timeout: 15_000 });

    await page.goto(experimentsUrl(slug));
    await expect(
      page.getByRole('heading', { name: 'Experiments', exact: true })
    ).toBeVisible({
      timeout: 15_000,
    });

    await page.goto(localizationUrl(slug));
    await expect(
      page.getByRole('heading', { name: 'Localization control room' })
    ).toBeVisible({
      timeout: 15_000,
    });

    await page.goto(scaleUrl(slug));
    await expect(
      page.getByRole('heading', { name: 'Scale', exact: true })
    ).toBeVisible({ timeout: 15_000 });
  });

  await test.step('Verify canonical snapshot matches pre-restart', async () => {
    const postRestartSnapshot = inspectCanonicalSnapshot({
      workspaceId: ws.workspaceId,
      ownerUserId,
      decisionId,
      batchId,
      enrollmentId,
      episodeId,
      metaMode: 'customer_sandbox',
    });

    // Compare stable fields (exclude timestamps)
    expect(postRestartSnapshot.library).toEqual(preRestartSnapshot.library);
    expect(postRestartSnapshot.generation).toEqual(
      preRestartSnapshot.generation
    );
    expect(postRestartSnapshot.assets).toEqual(preRestartSnapshot.assets);
    expect(postRestartSnapshot.winner).toEqual(preRestartSnapshot.winner);
    expect(postRestartSnapshot.localization).toEqual(
      preRestartSnapshot.localization
    );
    expect(postRestartSnapshot.scale).toEqual(preRestartSnapshot.scale);
    expect(postRestartSnapshot.zeroMetaWrites).toEqual(
      preRestartSnapshot.zeroMetaWrites
    );
    expect(postRestartSnapshot.crossAreaIds).toEqual(
      preRestartSnapshot.crossAreaIds
    );

    // Zero Meta writes still zero
    expect(postRestartSnapshot.zeroMetaWrites.offending).toEqual([]);

    // Meta write config unchanged by restart — still disabled
    expect(postRestartSnapshot.metaWriteConfig).toEqual(
      preRestartSnapshot.metaWriteConfig
    );
    expect(postRestartSnapshot.metaWriteConfig.metaWriteEnabled).toBe(false);
    expect(postRestartSnapshot.metaWriteConfig.metaWriteAccessToken).toBe('');

    // Evidence sync runs unchanged by restart
    expect(postRestartSnapshot.evidenceSyncRuns).toEqual(
      preRestartSnapshot.evidenceSyncRuns
    );

    // RunItem identity stable across restart
    expect(postRestartSnapshot.generation?.runItemIdentity).toEqual(
      preRestartSnapshot.generation?.runItemIdentity
    );

    // Audit event counts unchanged by restart
    expect(postRestartSnapshot.auditEvents).toEqual(
      preRestartSnapshot.auditEvents
    );

    // NetworkSpy still clean
    expect(spy.offendingRequests).toEqual([]);
  });
});
