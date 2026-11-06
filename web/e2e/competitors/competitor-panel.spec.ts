import fs from 'node:fs';
import path from 'node:path';
import { test, expect, type Locator, type Page } from '@playwright/test';
import { CompetitorDouble, PROJECT_ID } from './double';
import { shot } from './shots';

/**
 * The competitors area on the product understanding page (C-31 #274, spec
 * §9 and §13 scenarios 1, 2, 3, 4, 5, 10, 13, 14).
 *
 * Runs in the `chromium` project (signed-in Owner of `E2E_WORKSPACE_SLUG`).
 * The competitor and scan routes, and the marketing project the test opens,
 * are answered by `CompetitorDouble` (`./double.ts`): Controlled Mock, or a
 * controlled double of a Workspace with both keys. No provider key is read
 * and nothing external is called. `C31_SCREENSHOT_DIR` saves 390×844
 * screenshots of each state.
 */
const SLUG = process.env.E2E_WORKSPACE_SLUG ?? 'workspace-a';
const PAGE = (locale = '') =>
  `${locale}/w/${SLUG}/experiments?test=${PROJECT_ID}`;
const AD_LIBRARY =
  'https://www.facebook.com/ads/library/?active_status=active&view_all_page_id=100000000000016';
const OWN_PAGE = 'https://www.facebook.com/profile.php?id=100064000000001';

const LOCALES = path.join(__dirname, '..', '..', 'locales');
const copy = (locale: string): Record<string, string> =>
  JSON.parse(
    fs.readFileSync(path.join(LOCALES, locale, 'competitors.json'), 'utf8')
  );

/** The area never scrolls sideways, whatever the viewport. */
async function expectNoSidewaysScroll(area: Locator) {
  const overflow = await area.evaluate(
    (node) =>
      Array.from(node.querySelectorAll('*')).filter(
        (child) =>
          child.getBoundingClientRect().right >
          node.getBoundingClientRect().right + 1
      ).length
  );
  expect(overflow).toBe(0);
}

async function addCompetitor(
  area: Locator,
  input: string,
  options: { website?: string; self?: boolean } = {}
) {
  await area.getByLabel('Page link, Ad Library link or name').fill(input);
  if (options.website)
    await area.getByLabel('Official website (optional)').fill(options.website);
  if (options.self) await area.getByLabel('Role').selectOption('self');
  await area.getByRole('button', { name: 'Add', exact: true }).click();
}

/** Lists, resolves and confirms the three pages every scan below uses. */
async function buildList(
  page: Page,
  area: Locator,
  double: CompetitorDouble,
  label: string
) {
  await expect(area.getByText(/No competitors yet/)).toBeVisible();

  // Scenario 1: an Ad Library link confirms at once, without a resolve.
  await area.getByLabel('Page link, Ad Library link or name').fill(AD_LIBRARY);
  await expect(area.getByTestId('competitor-input-kind')).toHaveAttribute(
    'data-kind',
    'ad_library'
  );
  await area.getByRole('button', { name: 'Add', exact: true }).click();
  const brightvolt = area
    .getByTestId('competitor-row')
    .filter({ hasText: 'Brightvolt' });
  await expect(brightvolt).toHaveAttribute('data-confirmation', 'confirmed');
  await expect(brightvolt.getByText('Confirmed by link')).toBeVisible();
  await expect(brightvolt.getByText('Not scanned yet')).toBeVisible();

  // Scenario 2: a name with the official site goes through a paid resolve,
  // quoted before it starts; the domain check picks the right candidate.
  await addCompetitor(area, 'Acme Charge', { website: 'acme-charge.example' });
  const panel = area.getByTestId('competitor-resolve-panel');
  await expect(panel.getByTestId('competitor-resolve-cost')).toContainText(
    /Name search: 1 call · 1 credit/
  );
  await shot(page, `${label}-02-resolve-quote`, panel);
  await panel.getByRole('button', { name: 'Resolve', exact: true }).click();
  const card = panel.getByTestId('competitor-candidate-card');
  await expect(card).toHaveAttribute('data-page-id', '100000000000010');
  await expect(card.getByTestId('competitor-domain-hit')).toHaveText(
    'Ads link to acme-charge.example'
  );
  await expect(card.getByTestId('competitor-avatar-placeholder')).toBeVisible();
  await expect(panel.getByText('Candidate 1 of 3')).toBeVisible();
  await panel.getByRole('button', { name: 'Next' }).click();
  await expect(card).toHaveAttribute('data-page-id', '200000000000001');
  await expect(card.getByTestId('competitor-domain-hit')).toHaveText(
    'Ads do not link to acme-charge.example'
  );
  await shot(page, `${label}-03-candidate-card`, card);
  await panel.getByRole('button', { name: 'Previous' }).click();
  await panel.getByRole('button', { name: 'Confirm this page' }).click();
  const acme = area
    .getByTestId('competitor-row')
    .filter({ hasText: '@acmecharge' });
  await expect(acme).toHaveAttribute('data-confirmation', 'confirmed');

  // Scenario 14: the product's own page joins as the baseline.
  await addCompetitor(area, OWN_PAGE, { self: true });
  const own = area
    .getByTestId('competitor-row')
    .filter({ hasText: 'Your own page' });
  await expect(own).toHaveAttribute('data-role', 'self');
  await expect(own).toHaveAttribute('data-confirmation', 'confirmed');

  const operations = double.commands.map((c) => c.operation);
  expect(operations.filter((o) => o === 'resolve_competitor')).toHaveLength(1);
  return { brightvolt, acme, own };
}

async function waitForStatus(area: Locator, status: string) {
  await expect(area.getByTestId('competitor-scan-progress')).toHaveAttribute(
    'data-status',
    status,
    { timeout: 20_000 }
  );
}

test.describe('Competitors area (C-31 #274)', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
  });

  test('without keys: Controlled Mock from an empty product to a completed scan, resumed once', async ({
    page,
  }) => {
    test.setTimeout(120_000);
    const double = new CompetitorDouble({
      slug: SLUG,
      credentials: 'missing',
      scanOutcomes: ['partial'],
    });
    await double.install(page);
    await page.goto(PAGE());
    const area = page.getByTestId('competitors-area');
    await shot(page, 'mock-01-empty', area);
    const { brightvolt } = await buildList(page, area, double, 'mock');
    await expect(
      area.getByTestId('competitor-resolve-cost').first()
    ).toHaveCount(0);

    // Scenario 10: without the market-data key only Controlled Mock is
    // offered, the Owner is sent to Settings, and the run is marked simulated.
    const setup = area.getByTestId('competitor-scan-setup');
    const hint = setup.getByTestId('competitor-credential-hint');
    await expect(hint).toContainText('ScrapeCreators key');
    await expect(hint.getByTestId('competitor-settings-link')).toHaveAttribute(
      'href',
      `/w/${SLUG}/settings#provider-credentials`
    );
    await expect(setup.getByRole('radio')).toHaveCount(0);
    const quote = setup.getByTestId('competitor-scan-quote');
    await expect(quote).toHaveAttribute('data-mode', 'mock');
    await expect(setup.getByLabel('Market')).toHaveValue('US');
    await setup.getByLabel('Keywords (optional)').fill('NACS adapter');
    await expect(
      quote.getByTestId('competitor-scan-quote-provider')
    ).toContainText('Keyword pages: 2 calls · 2 credits');
    await expect(quote.getByTestId('competitor-scan-quote-llm')).toBeVisible();
    await expectNoSidewaysScroll(area);
    await shot(page, 'mock-04-scan-setup', hint);
    await setup.getByTestId('competitor-scan-start').click();

    // Progress by step, then scenario 4: credits ran out on the last page.
    const progress = area.getByTestId('competitor-scan-progress');
    await expect(
      progress.getByTestId('competitor-simulated-badge')
    ).toBeVisible();
    await expect(
      progress.getByTestId('competitor-scan-step-fetch')
    ).toBeVisible();
    await shot(page, 'mock-05-scan-progress', progress);
    await waitForStatus(area, 'partial');
    await expect(
      progress.getByText(
        'Some pages were not fetched. The ads already fetched are kept.'
      )
    ).toBeVisible();
    await expect(progress.getByText(/out of credits/).first()).toBeVisible();
    const resume = progress.getByTestId('competitor-scan-resume');
    await expect(resume.getByTestId('competitor-scan-quote')).toHaveAttribute(
      'data-kind',
      'resume'
    );
    await expect(resume).toContainText('Competitor pages: 1 call · 1 credit');
    await expectNoSidewaysScroll(area);
    await shot(page, 'mock-06-scan-partial-resume', resume);

    // Scenario 13: resume keeps the scan id and reserves only what is left.
    const scanId = [...double.scans.keys()][0];
    await resume.getByTestId('competitor-scan-resume-button').click();
    await waitForStatus(area, 'completed');
    await expect(
      progress.getByText(/Scan completed: \d+ ads observed\./)
    ).toBeVisible();
    await expect(progress.getByText('Resumed 1 time.')).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`competitor_scan=${scanId}`));
    await expect(brightvolt.getByTestId('competitor-last-scan')).toContainText(
      'Last scanned'
    );
    await shot(page, 'mock-07-scan-completed', progress);

    const commands = double.commands.map((c) => c.operation);
    expect(commands.filter((o) => o === 'create_competitor_scan')).toHaveLength(
      1
    );
    expect(commands.filter((o) => o === 'resume_competitor_scan')).toHaveLength(
      1
    );
    expect(
      double.commands.find((c) => c.operation === 'create_competitor_scan')!
        .body
    ).toMatchObject({ execution_mode: 'mock', keywords: ['NACS adapter'] });
    expect(double.scans.size).toBe(1);

    // The scan survives a reload through its query parameter.
    await page.reload();
    await waitForStatus(page.getByTestId('competitors-area'), 'completed');
  });

  test('with keys (controlled double): real-mode quote, an uncertain page reconciled by the Owner, then resumed', async ({
    page,
  }) => {
    test.setTimeout(120_000);
    const double = new CompetitorDouble({
      slug: SLUG,
      credentials: 'ready',
      scanOutcomes: ['uncertain'],
    });
    await double.install(page);
    await page.goto(PAGE());
    const area = page.getByTestId('competitors-area');
    await buildList(page, area, double, 'keys');

    // Scenario 3: both lines, real money, mode choice defaulting to the keys.
    const setup = area.getByTestId('competitor-scan-setup');
    await expect(setup.getByTestId('competitor-credential-hint')).toHaveCount(
      0
    );
    await expect(setup.getByRole('radio', { name: /Real data/ })).toBeChecked();
    const quote = setup.getByTestId('competitor-scan-quote');
    await expect(quote).toHaveAttribute('data-mode', 'byok');
    await expect(
      quote.getByTestId('competitor-scan-quote-provider')
    ).toContainText(/up to 19 credits · \$0\.03/);
    await expect(quote.getByTestId('competitor-scan-quote-llm')).toContainText(
      /up to \$/
    );
    await shot(page, 'keys-08-scan-setup', quote);
    await setup.getByRole('button', { name: /Start scan · up to \$/ }).click();

    // Scenario 5: the lost page waits for reconciliation; the automatic
    // check could not tell, so the Owner marks it.
    await waitForStatus(area, 'uncertain');
    const box = area.getByTestId('competitor-scan-reconciliation');
    await expect(
      box.getByTestId('competitor-scan-balance-check')
    ).toContainText('The credit balance moved by 2, not 0 or 1.');
    await shot(page, 'keys-09-scan-uncertain-reconcile', box);
    await box.getByRole('button', { name: 'Mark charged' }).click();
    await expect(
      box.getByTestId('competitor-scan-reconciliation-entry')
    ).toHaveAttribute('data-status', 'charged');
    await expect(box).toContainText('Settled as charged · by the Owner');

    const resume = area.getByTestId('competitor-scan-resume');
    await expect(resume.getByTestId('competitor-scan-quote')).toHaveAttribute(
      'data-mode',
      'byok'
    );
    await resume.getByRole('button', { name: /Resume · up to \$/ }).click();
    await waitForStatus(area, 'completed');
    await expect(
      area
        .getByTestId('competitor-scan-progress')
        .getByTestId('competitor-simulated-badge')
    ).toHaveCount(0);
    await shot(
      page,
      'keys-10-scan-completed',
      area.getByTestId('competitor-scan-progress')
    );

    expect(
      double.commands.find((c) => c.operation === 'create_competitor_scan')!
        .body
    ).toMatchObject({ execution_mode: 'byok' });
    expect(
      double.commands.find((c) => c.operation === 'reconcile_competitor_scan')!
        .body
    ).toMatchObject({ outcome: 'charged' });
  });

  test('switching to zh-CN leaves no English copy behind, and back', async ({
    browser,
  }) => {
    test.setTimeout(120_000);
    const en = copy('en');
    const zh = copy('zh-CN');
    // Fragments of each language's own copy (placeholders cut out).
    const fragments = (
      own: Record<string, string>,
      other: Record<string, string>
    ) =>
      Object.entries(own)
        .filter(([key, value]) => value !== other[key])
        .flatMap(([, value]) => value.split(/{{[^}]+}}/))
        .map((part) => part.trim())
        .filter((part) => part.length >= 8 && /[A-Za-z一-鿿]/.test(part));
    // A raw key on screen (`scan-status-partial`) means missing copy.
    const keyNames = Object.keys(en)
      .map((key) => key.replace(/_(one|other)$/, ''))
      .filter((key) => key.includes('-'));

    for (const [locale, prefix, foreign] of [
      ['zh-CN', '/zh-CN', fragments(en, zh)],
      ['en', '', fragments(zh, en)],
    ] as const) {
      const context = await browser.newContext({
        locale,
        storageState: 'e2e/.auth/owner.json',
        viewport: { width: 390, height: 844 },
      });
      const page = await context.newPage();
      const double = new CompetitorDouble({
        slug: SLUG,
        credentials: 'missing',
        scanOutcomes: ['partial'],
      });
      await double.install(page);
      await page.goto(PAGE(prefix));
      const area = page.getByTestId('competitors-area');
      await area.getByRole('textbox').first().fill(AD_LIBRARY);
      await area.locator('form button[type="submit"]').click();
      await expect(area.getByTestId('competitor-row')).toHaveCount(1);
      await area.getByRole('textbox').first().fill('Acme Charge');
      await area.locator('form button[type="submit"]').click();
      await expect(area.getByTestId('competitor-resolve-cost')).toBeVisible();
      await area.getByTestId('competitor-scan-start').click();
      await waitForStatus(area, 'partial');
      await expect(
        area
          .getByTestId('competitor-scan-resume')
          .getByTestId('competitor-scan-quote')
      ).toBeVisible();
      await shot(
        page,
        `locale-${locale}-partial`,
        area.getByTestId('competitor-scan-progress')
      );

      const text = await area.innerText();
      expect(foreign.filter((part) => text.includes(part))).toEqual([]);
      expect(keyNames.filter((key) => text.includes(key))).toEqual([]);
      if (locale === 'zh-CN') expect(text).toContain('竞品清单');
      else expect(text).toContain('Competitor list');
      await context.close();
    }
  });
});
