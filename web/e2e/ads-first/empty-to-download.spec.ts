/**
 * The new opening on the real stack (ADR 0018, design §10 step 1, #204): an
 * empty Workspace opens the start page; one click on Start reads a pasted
 * paragraph, drafts the claims, makes and checks the first ads without
 * another click; the user keeps two, checks them once and downloads exactly
 * those two images.
 *
 * Controlled Mock only, nothing reaches a paid provider: the start page
 * always asks for the user's own key (`byok`), so this test rewrites the
 * quote and the start request to `mock` in the browser -- the product code is
 * unchanged and the page still never offers Mock. Pasted text, not a link:
 * the Worker refuses private addresses and `*.example` does not resolve.
 *
 * Needs the web built with `NEXT_PUBLIC_MARKETAI_ADS_FIRST=true`
 * (`docker-compose.ads-first.yml`, `make ads-first-e2e`); it skips itself
 * otherwise.
 */
import { randomUUID } from 'crypto';
import { writeFileSync } from 'fs';
import type { Download, Page } from '@playwright/test';
import {
  expect,
  test,
  totpCode,
  provisionWorkspace,
  type WorkspaceProvisionResult,
} from '../golden-path/fixtures';
import { en } from '../support/copy';

const RUN_SUFFIX = `af-${randomUUID().slice(0, 8)}`;
const MFA_ENCRYPTION_KEY = Buffer.from(
  process.env.MFA_ENCRYPTION_KEY ??
    'Y29tcG9zZS1tZmEtZW5jcnlwdGlvbi1rZXktMzJiISE=',
  'base64'
);
const INTERNAL_ID =
  /\b(prod|lib|libver|gd|gr|av|pi|op|mp|mfb|run|pc)_[A-Za-z0-9]{6,}/;
/** A fictional product, pasted as text. */
const PRODUCT_TEXT =
  'Acme Charge Home is a wall-mounted home charger for electric cars. ' +
  'It charges at up to 7 kW, schedules charging from the Acme Charge app ' +
  'and has an IP55 weatherproof case for outdoor driveways.';

let ws: WorkspaceProvisionResult;
let prisma: any;
let domainDb: any;
let dispatchCurrentSnapshot: (
  teamId: string,
  dispatcherId: string
) => Promise<string>;

test.beforeAll(async () => {
  test.skip(
    process.env.E2E_ADS_FIRST !== 'true',
    'Needs the web built with NEXT_PUBLIC_MARKETAI_ADS_FIRST=true (make ads-first-e2e).'
  );
  const provisioned = await provisionWorkspace({
    suffix: RUN_SUFFIX,
    mfaEncryptionKey: MFA_ENCRYPTION_KEY,
  });
  ws = provisioned.result;
  prisma = provisioned.prisma;
  domainDb = provisioned.domainDb;
  dispatchCurrentSnapshot = provisioned.dispatchCurrentSnapshot;
});

test.afterAll(async () => {
  await prisma?.$disconnect();
  await domainDb?.$disconnect();
});

/**
 * Turns the first batch's quote and start into Controlled Mock: the page asks
 * for `byok`, the test makes it `mock`. Nothing else is touched.
 */
async function useControlledMock(page: Page) {
  await page.route('**/marketing/first-batch-quote*', (route) =>
    route.continue({
      url: route
        .request()
        .url()
        .replace('execution_mode=byok', 'execution_mode=mock'),
    })
  );
  await page.route('**/marketing/first-batch', async (route) => {
    const request = route.request();
    if (request.method() !== 'POST') return route.continue();
    const body = JSON.parse(request.postData() ?? '{}');
    return route.continue({
      postData: JSON.stringify({ ...body, execution_mode: 'mock' }),
    });
  });
}

/**
 * With `E2E_ADS_FIRST_SHOTS` set to a folder, keeps a screenshot of each
 * screen for the tutorial (#204); otherwise does nothing.
 */
async function shot(page: Page, name: string) {
  const dir = process.env.E2E_ADS_FIRST_SHOTS;
  if (!dir) return;
  // The view a person sees from the top of the page: a full-page capture
  // misplaces the sticky sidebar and the kept bar.
  await page.setViewportSize({ width: 1280, height: 1100 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: `${dir}/${name}.png` });
}

test('empty Workspace → start → ideas → check once → download the two kept images', async ({
  page,
}) => {
  test.setTimeout(900_000);
  const slug = ws.workspaceSlug;

  await test.step('the Owner signs up, enrolls MFA and the Workspace activates', async () => {
    await page.goto(
      `/auth/join?token=${encodeURIComponent(ws.invitationToken)}`
    );
    await page.locator('input[name="name"]').fill('Ads First Owner');
    await page.locator('input[name="password"]').fill(ws.ownerPassword);
    await page.getByRole('button', { name: 'Create Account' }).click();
    await page.waitForURL(/\/auth\/login\?callbackUrl=/);
    await page.locator('input[name="email"]').fill(ws.ownerEmail);
    await page.locator('input[name="password"]').fill(ws.ownerPassword);
    await page.getByRole('button', { name: /sign in/i }).click();
    await page.waitForURL(/\/settings\/security\?onboarding=/);
    await page.getByRole('button', { name: 'Set up MFA' }).click();
    const secret = (await page.locator('code').first().innerText()).trim();
    await page.getByLabel('Authenticator code').fill(totpCode(secret));
    await page.getByRole('button', { name: 'Confirm MFA' }).click();
    await expect(
      page.getByText(/Save these one-time recovery codes/)
    ).toBeVisible();
    await dispatchCurrentSnapshot(
      ws.workspaceId,
      `e2e-af-initial-${RUN_SUFFIX}`
    );
    await expect
      .poll(async () => {
        const team = await prisma.team.findUnique({
          where: { id: ws.workspaceId },
        });
        return team?.lifecycle;
      })
      .toBe('ACTIVE');
    await dispatchCurrentSnapshot(
      ws.workspaceId,
      `e2e-af-active-${RUN_SUFFIX}`
    );
  });

  await test.step('an empty Workspace opens the start page; without a key the Owner is asked for one, and the examples say so', async () => {
    await page.goto(`/w/${slug}`);
    await expect(page).toHaveURL(new RegExp(`/w/${slug}/start$`), {
      timeout: 30_000,
    });
    await expect(
      page.getByRole('heading', { level: 1, name: en('ideas', 'start-title') })
    ).toBeVisible();
    await expect(page.getByTestId('start-key-row')).toBeVisible();
    await expect(
      page.getByRole('button', { name: en('ideas', 'start-key-save') })
    ).toBeVisible();
    // The six static examples are marked as examples, never as the user's ads.
    await expect(
      page
        .getByTestId('start-examples')
        .getByText(en('ideas', 'start-example-badge'), { exact: true })
    ).toHaveCount(6);
    await shot(page, '01-start-no-key');
  });

  let readingUrl = '';

  await test.step('one click on Start, its maximum on the button, is the only confirmation', async () => {
    await useControlledMock(page);
    await page.reload();
    await page.getByLabel(en('ideas', 'start-paste-label')).fill(PRODUCT_TEXT);
    const start = page.getByRole('button', {
      name: en('ideas', 'start-button', { cost: '$0.00' }),
    });
    await expect(start).toBeEnabled({ timeout: 30_000 });
    await shot(page, '02-start');
    const started = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith('/marketing/first-batch') &&
        r.request().method() === 'POST'
    );
    await start.click();
    // The web proxy passes the answer through (#217: no 502 on start).
    expect((await started).status()).toBe(201);
    await page.waitForURL(new RegExp(`/w/${slug}/start/[^/]+$`));
    readingUrl = page.url();
  });

  await test.step('the reading stops once to choose the model; continuing costs nothing more here', async () => {
    const stop = page.getByTestId('reading-stop');
    await expect(stop).toHaveAttribute('data-reason', 'several_models', {
      timeout: 180_000,
    });
    await stop.getByRole('radio').first().check();
    await shot(page, '03-reading-choose-model');
    await stop
      .getByRole('button', { name: en('ideas', 'stop-several_models-action') })
      .click();
  });

  let ideasUrl = '';

  await test.step('facts, claims and ads appear with no further click; the page opens the ad ideas', async () => {
    await page.waitForURL(new RegExp(`/w/${slug}/p/[^/]+/ideas$`), {
      timeout: 600_000,
    });
    ideasUrl = page.url();
    await expect(page.getByTestId('ad-card').first()).toBeVisible();
    // The drafts' images load in the page (a Mock image is a drawn placeholder).
    const firstImage = page.getByTestId('ad-card').first().locator('img');
    await expect(firstImage).toBeVisible({ timeout: 60_000 });
    await expect
      .poll(() =>
        firstImage.evaluate((img) => (img as HTMLImageElement).naturalWidth)
      )
      .toBeGreaterThan(0);
    // Every ad finishes its automatic check before anything is downloaded.
    await expect(page.getByText(en('ideas', 'ideas-ad-checking'))).toHaveCount(
      0,
      { timeout: 300_000 }
    );
    await expect(page.locator('main')).not.toContainText(INTERNAL_ID);
    await shot(page, '04-ideas');
  });

  await test.step('keep two ads', async () => {
    const keep = page.getByRole('button', {
      name: en('ideas', 'ideas-ad-keep'),
    });
    await keep.nth(0).click();
    await expect(page.getByTestId('ideas-kept-bar')).toContainText(
      en('ideas', 'ideas-kept-count', { count: 1 })
    );
    await keep.nth(0).click();
    await expect(page.getByTestId('ideas-kept-bar')).toContainText(
      en('ideas', 'ideas-kept-count', { count: 2 })
    );
    await shot(page, '05-kept');
  });

  const downloads: Download[] = [];

  await test.step('download asks for the one check first; after it, exactly the two kept images download', async () => {
    await page
      .getByTestId('ideas-kept-bar')
      .getByRole('button', { name: en('ideas', 'ideas-download-images') })
      .click();
    const sheet = page.getByTestId('check-sheet');
    await expect(
      sheet.getByRole('heading', { name: en('ideas', 'check-title') })
    ).toBeVisible();
    const confirm = page.getByTestId('check-confirm-bar').getByRole('button', {
      name: en('ideas', 'check-confirm', { count: 2 }),
    });
    // Drafts: nothing downloads until every fact, look and finding is ticked.
    await expect(confirm).toBeDisabled();
    // Tick one at a time: a ticked item can settle others (a confirmed look
    // settles its "can't tell the look" finding), and a pressed finding
    // changes its own label.
    const unticked = sheet.locator('input[type="checkbox"]:not(:checked)');
    while ((await unticked.count()) > 0) await unticked.first().check();
    const open = sheet.getByRole('button', {
      name: en('ideas', 'check-finding-accept'),
      exact: true,
    });
    while ((await open.count()) > 0) await open.first().click();
    await expect(confirm).toBeEnabled();
    await shot(page, '06-check');
    page.on('download', (download) => downloads.push(download));
    await confirm.click();
    await expect.poll(() => downloads.length, { timeout: 60_000 }).toBe(2);
    for (const download of downloads)
      expect(download.suggestedFilename()).toMatch(/^ad-MK-[A-Z0-9]{6}\.png$/);
  });

  await test.step('refreshing keeps everything; the reading page still shows the finished batch', async () => {
    await page.goto(ideasUrl);
    await expect(page.getByTestId('ideas-kept-bar')).toContainText(
      en('ideas', 'ideas-kept-count', { count: 2 })
    );
    await page.goto(readingUrl);
    await page.waitForURL(new RegExp(`/w/${slug}/p/[^/]+/ideas$`), {
      timeout: 30_000,
    });
  });

  await test.step('the product appears on the overview, its profile and the competitors page', async () => {
    const productPath = new URL(ideasUrl).pathname.replace(/\/ideas$/, '');
    await page.goto(`/w/${slug}/overview`);
    await expect(page.getByTestId('overview-product')).toHaveCount(1);
    await shot(page, '07-overview');
    await page.goto(`${productPath}/profile`);
    await expect(page.getByTestId('profile-source')).toBeVisible();
    await shot(page, '08-profile');
    await page.goto(`/w/${slug}/competitors`);
    await expect(page.locator('main h1').first()).toBeVisible();
    await shot(page, '09-competitors');
  });

  const shots = process.env.E2E_ADS_FIRST_SHOTS;
  if (shots) {
    // For checking the same pages by hand afterwards: phone width, the other
    // language, dark mode. Kept with the screenshots, never in the repository.
    await page.context().storageState({ path: `${shots}/state.json` });
    writeFileSync(
      `${shots}/pages.json`,
      JSON.stringify({ slug, readingUrl, ideasUrl }, null, 2)
    );
  }

  await test.step('the advanced workbench and the tests made before the redesign still open', async () => {
    for (const area of ['experiments', 'generate', 'assets', 'library']) {
      const response = await page.goto(`/w/${slug}/${area}`);
      expect(response?.status(), area).toBeLessThan(400);
      await expect(page.locator('main h1').first()).toBeVisible();
    }
  });
});
