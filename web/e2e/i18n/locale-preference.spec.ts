/**
 * Interface language on the real stack (#252, ADR 0013): a signed-out page
 * follows the browser; the top-bar switch saves the account's preference and
 * reloads in that language; the language survives a refresh and a fresh
 * sign-in on a browser that asks for the other one; "Same as my browser" in
 * account settings hands the choice back to the browser.
 *
 * Runs in the `golden-path` Playwright project (same compose stack, own
 * Workspace).
 */
import { randomUUID } from 'crypto';
import type { Browser, Page } from '@playwright/test';
import {
  expect,
  test,
  totpCode,
  provisionWorkspace,
  type WorkspaceProvisionResult,
} from '../golden-path/fixtures';

const RUN_SUFFIX = `i18n-${randomUUID().slice(0, 8)}`;
const MFA_ENCRYPTION_KEY = Buffer.from(
  process.env.MFA_ENCRYPTION_KEY ??
    'Y29tcG9zZS1tZmEtZW5jcnlwdGlvbi1rZXktMzJiISE=',
  'base64'
);

let ws: WorkspaceProvisionResult;
let prisma: any;
let domainDb: any;
let mfaSecret = '';
let lastTotpStep = -1;

/** A TOTP code from a time step not used before in this run. */
async function freshTotp(page: Page) {
  while (Math.floor(Date.now() / 30_000) === lastTotpStep) {
    await page.waitForTimeout(1_000);
  }
  lastTotpStep = Math.floor(Date.now() / 30_000);
  return totpCode(mfaSecret);
}

async function signIn(page: Page, buttonName: RegExp) {
  await page.locator('input[name="email"]').fill(ws.ownerEmail);
  await page.locator('input[name="password"]').fill(ws.ownerPassword);
  await page.locator('input[name="mfaCode"]').fill(await freshTotp(page));
  await page.getByRole('button', { name: buttonName }).click();
}

async function browserAsking(browser: Browser, acceptLanguage: string) {
  const context = await browser.newContext({
    storageState: undefined,
    recordVideo: undefined,
    locale: acceptLanguage.split(',')[0],
    extraHTTPHeaders: { 'Accept-Language': acceptLanguage },
  });
  return { context, page: await context.newPage() };
}

test.beforeAll(async () => {
  const provisioned = await provisionWorkspace({
    suffix: RUN_SUFFIX,
    mfaEncryptionKey: MFA_ENCRYPTION_KEY,
  });
  ws = provisioned.result;
  prisma = provisioned.prisma;
  domainDb = provisioned.domainDb;
});

test.afterAll(async () => {
  await prisma?.$disconnect();
  await domainDb?.$disconnect();
});

test('the interface language follows the browser, then the saved preference', async ({
  page,
  browser,
}) => {
  test.setTimeout(300_000);
  const slug = ws.workspaceSlug;

  await test.step('A signed-out page follows the browser language', async () => {
    const { context, page: zhPage } = await browserAsking(
      browser,
      'zh-CN,zh;q=0.9,en;q=0.8'
    );
    try {
      await zhPage.goto('/auth/login');
      await expect(zhPage).toHaveURL(/\/zh-CN\/auth\/login/);
      await expect(zhPage.locator('html')).toHaveAttribute('lang', 'zh-CN');
      await expect(
        zhPage.getByRole('button', { name: '登录' }).first()
      ).toBeVisible();
    } finally {
      await context.close();
    }
  });

  await test.step('Owner signs up and enrolls MFA in English', async () => {
    await page.goto(
      `/auth/join?token=${encodeURIComponent(ws.invitationToken)}`
    );
    await page.locator('input[name="name"]').fill('Locale Owner');
    await page.locator('input[name="password"]').fill(ws.ownerPassword);
    await page.getByRole('button', { name: 'Create Account' }).click();
    await page.waitForURL(/\/auth\/login\?callbackUrl=/);
    await page.locator('input[name="email"]').fill(ws.ownerEmail);
    await page.locator('input[name="password"]').fill(ws.ownerPassword);
    await page.getByRole('button', { name: /sign in/i }).click();
    await page.waitForURL(/\/settings\/security\?onboarding=/);
    await page.getByRole('button', { name: 'Set up MFA' }).click();
    mfaSecret = (await page.locator('code').first().innerText()).trim();
    await page.getByLabel('Authenticator code').fill(await freshTotp(page));
    await page.getByRole('button', { name: 'Confirm MFA' }).click();
    await expect(
      page.getByText(/Save these one-time recovery codes/)
    ).toBeVisible();
    await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  });

  await test.step('The top-bar switch saves Chinese and reloads in it', async () => {
    await page.goto('/settings/account');
    await expect(page.getByRole('heading', { name: 'Language' })).toBeVisible();
    await page.getByTestId('language-switcher').first().selectOption('zh-CN');
    await expect(page).toHaveURL(/\/zh-CN\/settings\/account$/);
    await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
    await expect(page.getByRole('heading', { name: '语言' })).toBeVisible();
    const user = await prisma.user.findUnique({
      where: { email: ws.ownerEmail },
      select: { locale: true },
    });
    expect(user?.locale).toBe('zh-CN');
  });

  await test.step('A refresh keeps the language', async () => {
    await page.reload();
    await expect(page).toHaveURL(/\/zh-CN\/settings\/account$/);
    await expect(page.getByRole('heading', { name: '语言' })).toBeVisible();
    // A link without the prefix is moved to the saved language as well.
    await page.goto('/settings/security');
    await expect(page).toHaveURL(/\/zh-CN\/settings\/security$/);
  });

  await test.step('A fresh sign-in on an English browser comes back in Chinese', async () => {
    const { context, page: enPage } = await browserAsking(
      browser,
      'en-US,en;q=0.9'
    );
    try {
      await enPage.goto(`/w/${slug}/products`);
      // Signed out, the English browser decides…
      await expect(enPage).toHaveURL(/^[^?]*\/auth\/login\?callbackUrl=/);
      await expect(enPage.locator('html')).toHaveAttribute('lang', 'en');
      await signIn(enPage, /sign in/i);
      // …signed in, the saved preference does.
      await expect(enPage).toHaveURL(/\/zh-CN\/(w\/|settings)/, {
        timeout: 15_000,
      });
      await expect(enPage.locator('html')).toHaveAttribute('lang', 'zh-CN');
      const cookies = await context.cookies();
      expect(cookies.find((c) => c.name === 'NEXT_LOCALE')?.value).toBe(
        'zh-CN'
      );
    } finally {
      await context.close();
    }
  });

  await test.step('"Same as my browser" hands the choice back to the browser', async () => {
    await page.goto('/zh-CN/settings/account');
    await page.getByTestId('account-language').selectOption('');
    await page
      .locator('form')
      .filter({ has: page.getByTestId('account-language') })
      .getByRole('button', { name: '保存修改' })
      .click();
    // The test browser asks for English.
    await expect(page).toHaveURL(/\/settings\/account$/);
    await expect(page).not.toHaveURL(/\/zh-CN\//);
    await expect(page.getByRole('heading', { name: 'Language' })).toBeVisible();
    const user = await prisma.user.findUnique({
      where: { email: ws.ownerEmail },
      select: { locale: true },
    });
    expect(user?.locale).toBeNull();
  });
});
