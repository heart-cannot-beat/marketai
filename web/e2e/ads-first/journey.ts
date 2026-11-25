/**
 * What the new opening's end-to-end suites share (ADR 0018, #204; step 2,
 * #259): the fictional product they paste, the Owner's sign-up, Controlled
 * Mock for the first batch, the tutorial screenshots and the internal-ID
 * guard. Each suite provisions its own Workspace.
 */
import type { Locator, Page } from '@playwright/test';
import {
  expect,
  totpCode,
  provisionWorkspace,
  type WorkspaceProvisionResult,
} from '../golden-path/fixtures';

export const MFA_ENCRYPTION_KEY = Buffer.from(
  process.env.MFA_ENCRYPTION_KEY ??
    'Y29tcG9zZS1tZmEtZW5jcnlwdGlvbi1rZXktMzJiISE=',
  'base64'
);

/** An internal id the main flow must never show. */
export const INTERNAL_ID =
  /\b(prod|lib|libver|gd|gr|av|pi|op|mp|mfb|run|pc)_[A-Za-z0-9]{6,}/;

/** A fictional product, pasted as text. */
export const PRODUCT_TEXT =
  'Acme Charge Home is a wall-mounted home charger for electric cars. ' +
  'It charges at up to 7 kW, schedules charging from the Acme Charge app ' +
  'and has an IP55 weatherproof case for outdoor driveways.';

export type AdsFirstWorkspace = Awaited<ReturnType<typeof provisionWorkspace>>;

export const SKIP_REASON =
  'Needs the web built with NEXT_PUBLIC_MARKETAI_ADS_FIRST=true (make ads-first-e2e).';

export function adsFirstEnabled(): boolean {
  return process.env.E2E_ADS_FIRST === 'true';
}

export function provisionAdsFirst(suffix: string): Promise<AdsFirstWorkspace> {
  return provisionWorkspace({ suffix, mfaEncryptionKey: MFA_ENCRYPTION_KEY });
}

/** The invited Owner signs up, enrolls MFA, and the Workspace activates. */
export async function signUpOwner(
  page: Page,
  provisioned: AdsFirstWorkspace,
  suffix: string
) {
  const ws: WorkspaceProvisionResult = provisioned.result;
  await page.goto(`/auth/join?token=${encodeURIComponent(ws.invitationToken)}`);
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
  await provisioned.dispatchCurrentSnapshot(
    ws.workspaceId,
    `e2e-af-initial-${suffix}`
  );
  await expect
    .poll(async () => {
      const team = await provisioned.prisma.team.findUnique({
        where: { id: ws.workspaceId },
      });
      return team?.lifecycle;
    })
    .toBe('ACTIVE');
  await provisioned.dispatchCurrentSnapshot(
    ws.workspaceId,
    `e2e-af-active-${suffix}`
  );
}

/**
 * Turns the first batch's quote and start into Controlled Mock: the page asks
 * for `byok`, the test makes it `mock`. Nothing else is touched. A test taken
 * from those ads runs the way its ads were made, so it is Controlled Mock too
 * and its own requests carry no mode to rewrite.
 */
export async function useControlledMock(page: Page) {
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
 * With `E2E_ADS_FIRST_SHOTS` set to a folder, keeps a screenshot of a screen
 * for the tutorial (#204, #259); otherwise does nothing. The view a person
 * sees from the top of the page, or from `at` when the part that matters is
 * further down: a full-page capture misplaces the sticky sidebar and bars.
 */
export async function shot(page: Page, name: string, at?: Locator) {
  const dir = process.env.E2E_ADS_FIRST_SHOTS;
  if (!dir) return;
  await page.setViewportSize({ width: 1280, height: 1100 });
  if (at)
    await at.evaluate((element) => element.scrollIntoView({ block: 'start' }));
  else await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: `${dir}/${name}.png` });
}
