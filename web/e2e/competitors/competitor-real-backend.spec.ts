import { test, expect, type Locator } from '@playwright/test';
import { CompetitorDouble, PROJECT_ID } from './double';
import { SLUG, adLibrary, createProduct } from './real-backend';
import { shot } from './shots';

/**
 * The competitors area against the real C-13 backend (#269) in Controlled
 * Mock: a real product, the real link confirmation and the real
 * `competitor.resolve` Job on the Worker (recorded Acme Charge / Brightvolt fixtures).
 * Only the marketing project the page opens and, until the C-21 Worker
 * (#270) ships, the scan routes come from `CompetitorDouble`.
 *
 * Needs the compose stack with its Worker and no market-data key, so it is
 * opt-in: `C31_REAL_BACKEND=1`.
 */
const AD_LIBRARY =
  'https://www.facebook.com/ads/library/?active_status=active&view_all_page_id=100000000000016';

test.skip(
  !process.env.C31_REAL_BACKEND,
  'needs the compose stack with the C-13 Worker'
);

test('real C-13 backend in Controlled Mock: link confirmation, quoted resolve and the scan double', async ({
  page,
}) => {
  test.setTimeout(240_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/w/${SLUG}/experiments`);
  const productId = await createProduct(page);
  const double = new CompetitorDouble({
    slug: SLUG,
    credentials: 'missing',
    scanOutcomes: ['partial'],
    productId,
    realCompetitors: true,
  });
  await double.install(page);
  await page.goto(`/w/${SLUG}/experiments?test=${PROJECT_ID}`);
  const area = page.getByTestId('competitors-area');
  await expect(area.getByText(/No competitors yet/)).toBeVisible();

  // Scenario 1 on the real backend: confirmed by the link, no resolve.
  await area.getByLabel('Page link, Ad Library link or name').fill(AD_LIBRARY);
  await area.getByRole('button', { name: 'Add', exact: true }).click();
  const linked = area.getByTestId('competitor-row').first();
  await expect(linked).toHaveAttribute('data-confirmation', 'confirmed');
  await expect(linked.getByText('Confirmed by link')).toBeVisible();

  // Scenario 2 on the real backend: the quote (page_search × 1 + probe × 3)
  // before the paid resolve; the recorded search and the domain probe
  // settle on `acmecharge`.
  await area
    .getByLabel('Page link, Ad Library link or name')
    .fill('Acme Charge');
  await area
    .getByLabel('Official website (optional)')
    .fill('acme-charge.example');
  await area.getByRole('button', { name: 'Add', exact: true }).click();
  const panel = area.getByTestId('competitor-resolve-panel');
  const cost = panel.getByTestId('competitor-resolve-cost');
  await expect(cost).toHaveAttribute('data-mode', 'mock');
  await expect(cost).toContainText('Name search: 1 call · 1 credit');
  await expect(cost).toContainText(
    'Candidate ad checks: up to 3 calls · 3 credits'
  );
  await panel.getByRole('button', { name: 'Resolve', exact: true }).click();
  const status = panel.getByTestId('competitor-resolution-status');
  await expect(status).toHaveAttribute('data-status', 'completed', {
    timeout: 90_000,
  });
  const position = await panel.getByText(/^Candidate \d+ of \d+$/).innerText();
  expect(Number(position.match(/of (\d+)/)![1])).toBeGreaterThanOrEqual(5);
  const card = panel.getByTestId('competitor-candidate-card');
  await expect(card).toHaveAttribute('data-page-id', '100000000000010');
  await expect(card.getByTestId('competitor-domain-hit')).toHaveAttribute(
    'data-hit',
    'true'
  );
  // The resolve archived the Mock's placeholder avatar; the card reads the
  // copy through a blob URL (#332), so `img-src` needs no provider host.
  await expect(card.getByTestId('competitor-avatar')).toHaveAttribute(
    'src',
    /^blob:/
  );
  await shot(page, 'real-03-resolve-candidate', card);
  const acme = area
    .getByTestId('competitor-row')
    .filter({ hasText: 'acme-charge.example' });
  if (await panel.getByText(/^Confirmed automatically/).isVisible()) {
    await expect(acme.getByTestId('competitor-confirmation-status')).toHaveText(
      'Confirmed automatically'
    );
  } else {
    await panel.getByRole('button', { name: 'Confirm this page' }).click();
  }
  await expect(acme).toHaveAttribute('data-confirmation', 'confirmed');

  // The scan double runs on the real competitors.
  const setup = area.getByTestId('competitor-scan-setup');
  await expect(setup.getByTestId('competitor-credential-hint')).toBeVisible();
  await setup.getByTestId('competitor-scan-start').click();
  await expect(area.getByTestId('competitor-scan-progress')).toHaveAttribute(
    'data-status',
    'partial',
    { timeout: 20_000 }
  );
  await area.getByTestId('competitor-scan-resume-button').click();
  await expect(area.getByTestId('competitor-scan-progress')).toHaveAttribute(
    'data-status',
    'completed',
    { timeout: 20_000 }
  );
  await expect(acme.getByTestId('competitor-last-scan')).toContainText(
    'Last scanned'
  );
});

/**
 * One scan target row. With no ads there is no board, so the row sits too
 * close to the page's end for `shot` to scroll it under the header.
 */
async function rowShot(row: Locator, name: string) {
  const dir = process.env.C31_SCREENSHOT_DIR;
  if (dir) await row.screenshot({ path: `${dir}/${name}.png` });
}

test('a page ID no scan has seen: the scan target and the list row say it may be wrong (#355)', async ({
  page,
  browser,
}) => {
  test.setTimeout(300_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/w/${SLUG}/experiments`);
  const productId = await createProduct(page);
  const double = new CompetitorDouble({
    slug: SLUG,
    credentials: 'missing',
    scanOutcomes: [],
    productId,
    realCompetitors: true,
    realScans: true,
  });
  await double.install(page);
  await page.goto(`/w/${SLUG}/experiments?test=${PROJECT_ID}`);
  const area = page.getByTestId('competitors-area');

  // Not a page P0 recorded: the Mock answers it the provider's empty page,
  // as the real provider answers a wrong ID (0 ads, no charge).
  const wrong = '100000000000001';
  await area
    .getByLabel('Page link, Ad Library link or name')
    .fill(adLibrary(wrong));
  await area.getByRole('button', { name: 'Add', exact: true }).click();
  const row = area.getByTestId('competitor-row').filter({ hasText: wrong });
  await expect(row).toHaveAttribute('data-confirmation', 'confirmed');
  await expect(row.getByTestId('competitor-name-after-scan')).toHaveText(
    'A link confirms the page without its name; it shows after the first scan.'
  );

  const setup = area.getByTestId('competitor-scan-setup');
  await expect(setup.getByTestId('competitor-scan-quote')).toHaveAttribute(
    'data-mode',
    'mock'
  );
  await setup.getByTestId('competitor-scan-start').click();
  const progress = area.getByTestId('competitor-scan-progress');
  await expect(progress).toHaveAttribute('data-status', 'completed', {
    timeout: 180_000,
  });
  const target = progress.getByTestId('competitor-scan-target');
  // The empty first page ends the page's fetch: one page of the three planned.
  await expect(target).toContainText('Done · 1/3 pages · 0 ads');
  const hint = target.getByTestId('competitor-scan-target-empty');
  await expect(hint).toContainText(
    'No ads came back. The page may not be running ads right now, or the page ID may be wrong.'
  );
  await expect(
    hint.getByRole('link', { name: 'View in the Meta Ad Library' })
  ).toHaveAttribute(
    'href',
    `https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=US&view_all_page_id=${wrong}`
  );
  // The list follows the scan: its row stops promising a name.
  await expect(row.getByTestId('competitor-name-after-scan')).toHaveText(
    'Scanned, but no ads from this page came back, so it has no name. If the page ID is wrong, change the page.'
  );
  await rowShot(target, 'real-04-empty-page-hint');
  await hint.getByRole('button', { name: 'Change page' }).click();
  await expect(row.getByTestId('competitor-resolve-panel')).toBeVisible();

  const zh = await browser.newContext({
    locale: 'zh-CN',
    viewport: { width: 390, height: 844 },
    storageState: 'e2e/.auth/owner.json',
  });
  const zhPage = await zh.newPage();
  await double.install(zhPage);
  await zhPage.goto(page.url());
  const zhArea = zhPage.getByTestId('competitors-area');
  await expect(zhArea.getByTestId('competitor-name-after-scan')).toHaveText(
    '已扫描，但没有看到这个主页的广告，所以没有名称；如果主页 ID 有误，请更换主页。'
  );
  const zhTarget = zhArea.getByTestId('competitor-scan-target');
  await expect(
    zhTarget.getByTestId('competitor-scan-target-empty')
  ).toContainText(
    '没有返回广告。可能这个主页现在没有在投广告，也可能主页 ID 填错了。'
  );
  await expect(
    zhTarget.getByRole('button', { name: '更换主页' })
  ).toBeVisible();
  await rowShot(zhTarget, 'real-04-empty-page-hint-zh');
  await zh.close();
});
