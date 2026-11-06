/**
 * Front-flow "设置产品" and "设置广告测试" on the real stack (Issues #162 and
 * #165, contract §3.1 / §3.2 / §7-1): an empty Workspace lands on the
 * product home, a pasted paragraph becomes an AI product card (controlled
 * Mock LLM -- no OpenAI key is bound), the user picks one of its colour
 * editions under "Choose a variant" (Issue #192: the edition's own colour and specs
 * replace the shared ones) and confirms. The legacy guided setup's URL
 * (`/tests/new`) now opens the marketing test: that flow is frozen (#337). The
 * product card's "Start test" opens the marketing test (J-32 #230) with the
 * confirmed card carried, and its fixed top area walks one main action at a
 * time to the first image on the board. No internal id is visible at any point, and the
 * v9 Generate workbench stays reachable through the folded "Advanced
 * workbench" navigation.
 *
 * Runs in the `golden-path` Playwright project (same compose stack, own
 * Workspace) so CI exercises it with the Worker processing the intake,
 * drafting and generation jobs.
 */
import { randomUUID } from 'crypto';
import {
  expect,
  test,
  totpCode,
  provisionWorkspace,
  type WorkspaceProvisionResult,
} from '../golden-path/fixtures';

const RUN_SUFFIX = `pi-${randomUUID().slice(0, 8)}`;
const MFA_ENCRYPTION_KEY = Buffer.from(
  process.env.MFA_ENCRYPTION_KEY ??
    'Y29tcG9zZS1tZmEtZW5jcnlwdGlvbi1rZXktMzJiISE=',
  'base64'
);
const INTERNAL_ID =
  /\b(prod|lib|libver|gd|gr|av|pi|atp|atd|op|mp|mr|mround|mgl|pc)_[A-Za-z0-9]{6,}|direction=|\bdir-\d/;

let ws: WorkspaceProvisionResult;
let prisma: any;
let domainDb: any;
let dispatchCurrentSnapshot: (
  teamId: string,
  dispatcherId: string
) => Promise<string>;

test.beforeAll(async () => {
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

test('paste a paragraph → product card; the old guided setup opens the marketing test; Start test → first image on the board', async ({
  page,
}) => {
  test.setTimeout(600_000);
  const slug = ws.workspaceSlug;

  await test.step('Owner signs up, enrolls MFA and the Workspace activates', async () => {
    await page.goto(
      `/auth/join?token=${encodeURIComponent(ws.invitationToken)}`
    );
    await page.locator('input[name="name"]').fill('Product Intake Owner');
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
      `e2e-pi-initial-${RUN_SUFFIX}`
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
      `e2e-pi-active-${RUN_SUFFIX}`
    );
  });

  await test.step('Workspace entry opens marketing tests and retains the product home', async () => {
    await page.goto(`/w/${slug}`);
    await expect(page).toHaveURL(new RegExp(`/w/${slug}/experiments$`), {
      timeout: 30_000,
    });
    await page.goto(`/w/${slug}/products`);
    await expect(
      page.getByRole('heading', { level: 1, name: 'Products' })
    ).toBeVisible();
    await expect(page.getByText('Add your first product')).toBeVisible();
    // The existing product workflow remains available from navigation.
    await expect(
      page.locator('nav a').filter({ hasText: 'Products' }).first()
    ).toHaveAttribute('href', `/w/${slug}/products`);
  });

  let productName = '';

  await test.step('Paste a paragraph and get a product card', async () => {
    await page.getByRole('link', { name: 'Add product' }).click();
    await expect(page).toHaveURL(new RegExp(`/w/${slug}/products/new$`));

    await page.getByLabel('Product link or description').fill(
      // "配色" makes the controlled Mock answer with three colour editions
      // (Issue #192), so the card opens on "Choose a variant".
      'VoltNest 家用交流充电桩，壁挂式，Type 2 接口，有曜石黑、冰川白、丛林迷彩三种配色，支持 App 预约谷电充电。'
    );
    const created = page.waitForResponse(
      (r) =>
        r.url().includes('/products/intake') &&
        r.request().method() === 'POST' &&
        r.status() === 202
    );
    await page.getByRole('button', { name: 'Read product' }).click();
    await created;

    // Contract §7-1: the card within 30 seconds.
    await expect(
      page.getByRole('heading', { name: 'Check the product card' })
    ).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId('intake-mock-badge')).toHaveText(
      'Example result, no OpenAI key bound'
    );
    productName = await page.getByLabel('Product name').inputValue();
    expect(productName.trim().length).toBeGreaterThan(0);
    expect(await page.locator('main').innerText()).not.toMatch(INTERNAL_ID);
  });

  await test.step('Pick the colour edition first: its colour and specs replace the shared ones', async () => {
    const choose = page.getByRole('region', { name: 'Choose a variant' });
    await expect(choose.getByTestId('variant-choice')).toHaveCount(3);
    const confirm = page.getByRole('button', { name: 'Confirm product' });
    await expect(confirm).toBeDisabled();
    const mustMatch = page.getByRole('region', {
      name: 'Appearance that must match',
    });
    await expect(mustMatch.getByRole('textbox').first()).toHaveValue(
      /可选配色/
    );

    await choose.getByRole('radio', { name: '冰川白' }).check();
    await expect(confirm).toBeEnabled();
    const colourItems = mustMatch.getByRole('textbox');
    await expect(colourItems.last()).toHaveValue(/亮面白色/);
    for (const value of await colourItems.evaluateAll((inputs) =>
      inputs.map((input) => (input as HTMLInputElement).value)
    )) {
      expect(value).not.toMatch(/可选配色|迷彩|曜石黑/);
    }
    const specValues = await page
      .getByRole('region', { name: 'Specs' })
      .getByRole('textbox', { name: /^Value/ })
      .evaluateAll((inputs) =>
        inputs.map((input) => (input as HTMLInputElement).value)
      );
    expect(specValues).toContain('亮面冰川白 PC+ABS 阻燃外壳');
  });

  await test.step('Edit and confirm', async () => {
    productName = `${productName}（E2E）`;
    await page.getByLabel('Product name').fill(productName);
    await page.getByRole('button', { name: 'Add a selling point' }).click();
    // `exact`: the remove button's label "Remove selling point 5" also contains it.
    await page.getByLabel('Selling point 5', { exact: true }).fill('五年质保');

    const confirmed = page.waitForResponse(
      (r) =>
        r.url().includes('/confirm') &&
        r.request().method() === 'POST' &&
        r.status() === 200
    );
    await page.getByRole('button', { name: 'Confirm product' }).click();
    await confirmed;

    await expect(page).toHaveURL(new RegExp(`/w/${slug}/products$`), {
      timeout: 15_000,
    });
    await expect(page.getByText(`Added “${productName}”`)).toBeVisible();
    const card = page.getByTestId('product-card').filter({
      has: page.getByRole('heading', { name: productName }),
    });
    await expect(card).toBeVisible();
    // Only the chosen edition entered the product.
    await expect(card.getByText('冰川白')).toBeVisible();
    await expect(card.getByText('曜石黑')).toHaveCount(0);
    await expect(card.getByRole('link', { name: 'Start test' })).toBeVisible();
    expect(await page.locator('main').innerText()).not.toMatch(INTERNAL_ID);
  });

  await test.step('Refresh keeps the product', async () => {
    await page.reload();
    await expect(page.getByRole('heading', { name: productName })).toBeVisible({
      timeout: 15_000,
    });
  });

  await test.step('The legacy guided setup URL opens the marketing test (#337)', async () => {
    // The old flow is view-only: its setup page starts no plan, with or
    // without a product deep link.
    await page.goto(`/w/${slug}/tests/new`);
    await expect(page).toHaveURL(new RegExp(`/w/${slug}/experiments$`), {
      timeout: 15_000,
    });
    await page.goto(`/w/${slug}/tests/new?product=anything`);
    await expect(page).toHaveURL(new RegExp(`/w/${slug}/experiments$`), {
      timeout: 15_000,
    });
    await expect(
      page.getByRole('heading', { level: 1, name: 'Set up an ad test' })
    ).toHaveCount(0);
    expect(await page.locator('main').innerText()).not.toMatch(INTERNAL_ID);
  });

  await test.step('The v9 Generate workbench is one fold away', async () => {
    const toggle = page.getByTestId('workspace-nav-advanced-toggle');
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await toggle.click();
    await page.locator('nav a', { hasText: 'New generation' }).click();
    await expect(page).toHaveURL(new RegExp(`/w/${slug}/generate`));
    await page.getByRole('button', { name: 'New Draft' }).first().click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible({ timeout: 15_000 });
    // The only published product is preselected.
    await expect(
      dialog.getByRole('radio', { name: productName })
    ).toBeChecked();
    expect(await dialog.innerText()).not.toMatch(INTERNAL_ID);
  });

  // J-32: one entry. Clicks are the presses needed to move on (the bar's
  // steps into a section are optional, as are fact checkboxes and the
  // delivery-mode choice); the confirmations are the three the spec keeps
  // (§8.2): the AI understanding, the test plan and the generation cost.
  let clicks = 0;
  let confirmations = 0;
  const focus = page.getByTestId('marketing-focus');

  await test.step('Start test on the product card opens the marketing test with the card carried', async () => {
    await page.goto(`/w/${slug}/products`);
    await page
      .getByTestId('product-card')
      .filter({ has: page.getByRole('heading', { name: productName }) })
      .getByRole('link', { name: 'Start test' })
      .click();
    clicks += 1;
    await expect(page).toHaveURL(new RegExp(`/w/${slug}/experiments\\?test=`), {
      timeout: 15_000,
    });
    await expect(
      focus.getByRole('heading', { level: 1, name: productName })
    ).toBeVisible();
    await expect(focus).toContainText('Business goal');
    await expect(focus).toContainText("This round's question");
    // The confirmed card is the test's product information, without ids.
    await focus.getByText('More', { exact: true }).click();
    await focus
      .getByRole('button', { name: 'Update product information' })
      .click();
    const source = await page
      .locator('#marketing-source')
      .getByLabel('Product details')
      .inputValue();
    expect(source.split('\n')[0]).toBe(productName);
    expect(source).toContain('- 五年质保');
    expect(source).not.toMatch(INTERNAL_ID);
    expect(await page.locator('main').innerText()).not.toMatch(INTERNAL_ID);
  });

  await test.step('The understanding is drafted and confirmed from the top area', async () => {
    // No planning key is bound: the controlled Mock example is chosen explicitly.
    await page.getByLabel('Try a controlled Mock example').check();
    clicks += 1;
    await focus
      .getByRole('button', { name: /Draft product understanding/ })
      .click();
    clicks += 1;
    await expect(
      page.getByRole('heading', {
        name: 'Does this describe your product accurately?',
      })
    ).toBeVisible({ timeout: 120_000 });
    for (const fact of await page
      .getByLabel('This fact and its evidence are accurate')
      .all())
      await fact.check();
    await focus
      .getByRole('button', {
        name: /Confirm understanding and draft the test plan/,
      })
      .click();
    confirmations += 1;
    await expect(
      page.getByRole('heading', { name: 'What will this test answer?' })
    ).toBeVisible({ timeout: 120_000 });
    expect(await page.locator('main').innerText()).not.toMatch(INTERNAL_ID);
  });

  await test.step('Confirming the plan prepares the drafts; the round starts', async () => {
    await expect(focus).not.toContainText('Set when the test plan is drafted');
    await focus
      .getByRole('button', { name: 'Confirm plan and prepare creatives' })
      .click();
    confirmations += 1;
    await expect(
      page.getByRole('group', { name: 'Creative plans' })
    ).toBeVisible({ timeout: 30_000 });
    // The bar steps to the round settings; scrolling there works as well.
    await focus
      .getByRole('button', { name: 'Go to the round settings' })
      .click();
    const freeze = page.locator('#marketing-freeze');
    // Mock creatives only count in a simulated round.
    await freeze.getByLabel('Delivery mode').selectOption('mock');
    await freeze
      .getByRole('button', { name: 'Confirm rules and review creatives' })
      .click();
    clicks += 1;
    await expect(focus).toContainText(/Images still to generate: \d+\./, {
      timeout: 30_000,
    });
  });

  await test.step('Preview the cost, confirm, and the first image is on the board', async () => {
    await focus
      .getByRole('button', { name: 'Preview the cost and generate' })
      .click();
    const editor = page.locator('#marketing-generation-editor');
    await editor
      .getByRole('button', { name: 'Preview and review cost' })
      .click();
    clicks += 1;
    await editor.getByRole('button', { name: 'Confirm and generate' }).click();
    confirmations += 1;
    await expect(page.locator('#marketing-board img').first()).toBeVisible({
      timeout: 240_000,
    });
    // Measured path (contract §8.2 keeps three confirmations).
    expect(confirmations).toBe(3);
    expect(clicks).toBeLessThanOrEqual(5);
    expect(await page.locator('main').innerText()).not.toMatch(INTERNAL_ID);
  });
});
