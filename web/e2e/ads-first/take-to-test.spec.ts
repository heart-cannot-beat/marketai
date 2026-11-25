/**
 * The new opening, step 2 on the real stack (ADR 0019, design §5 and §7,
 * #259): two kept ads are taken to a test. The quote says first that a small
 * budget only gives a rough read and what a verdict needs; three answers
 * (plus the landing page, as the product has no link) and one click start
 * the test; the look-together checks run, and the package is made only once
 * the user has handled what they found. A full report generated here comes
 * back twice; the short keys in its ad names prefill the matches, confirmed
 * once; the numbers so far, the result, the test history and the overview
 * follow. The round ends early and its minimal result says how many orders
 * are still short.
 *
 * Controlled Mock only, nothing reaches a paid provider: the first batch is
 * rewritten to `mock` in the browser as in step 1, and a test taken from
 * those ads runs the way its ads were made, so its quote and start carry no
 * mode and the test asserts the quote says `mock`. The report is a CSV
 * written here for the fictional Acme Charge product.
 *
 * Needs the web built with `NEXT_PUBLIC_MARKETAI_ADS_FIRST=true`
 * (`docker-compose.ads-first.yml`, `make ads-first-e2e`); it skips itself
 * otherwise.
 */
import { randomUUID } from 'crypto';
import { mkdtempSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import path from 'path';
import type { Download, Page } from '@playwright/test';
import { expect, test } from '../golden-path/fixtures';
import { en } from '../support/copy';
import {
  INTERNAL_ID,
  PRODUCT_TEXT,
  SKIP_REASON,
  adsFirstEnabled,
  provisionAdsFirst,
  shot,
  signUpOwner,
  useControlledMock,
  type AdsFirstWorkspace,
} from './journey';

const RUN_SUFFIX = `tt-${randomUUID().slice(0, 8)}`;
const TARGET_COST = 30;
const MIN_PURCHASES = 20;
const LANDING_PAGE = 'https://acmecharge.example/home';
const AD_ACCOUNT = 'act_1234';
/** A Meta ad in the same account that is not part of this test. */
const OTHER_AD = { id: '3003', name: 'Acme Charge Home retargeting' };
/** The ads went live 30 days ago, not on the planned start (tomorrow). */
const WENT_LIVE = -30;

let provisioned: AdsFirstWorkspace;

// The ad account's time zone defaults to the browser's: UTC keeps the planned
// days, the report's days and the assertions below on one calendar.
test.use({ timezoneId: 'UTC' });

test.beforeAll(async () => {
  test.skip(!adsFirstEnabled(), SKIP_REASON);
  provisioned = await provisionAdsFirst(RUN_SUFFIX);
});

test.afterAll(async () => {
  await provisioned?.prisma?.$disconnect();
  await provisioned?.domainDb?.$disconnect();
});

/** A calendar day `offset` days from today (UTC), as Meta's report writes it. */
function isoDay(offset: number): string {
  const day = new Date();
  day.setUTCDate(day.getUTCDate() + offset);
  return day.toISOString().slice(0, 10);
}

/** A calendar day as the test pages write it in English. */
function dayText(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString('en-US', {
    timeZone: 'UTC',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  });
}

/** Whole US dollars as the test pages write them. */
function usd(amount: number): string {
  return `$${amount.toLocaleString('en-US')}`;
}

interface ReportRow {
  day: string;
  adId: string;
  adName: string;
  spend: number;
  purchases: number;
}

/** Meta's daily ad report, from the day the ads went live to `days` later. */
function reportRows(
  ads: { adId: string; adName: string }[],
  days: number,
  daily: (adIndex: number, day: number) => { spend: number; purchases: number }
): ReportRow[] {
  const rows: ReportRow[] = [];
  for (let day = 0; day < days; day++)
    ads.forEach((ad, index) =>
      rows.push({ ...ad, day: isoDay(WENT_LIVE + day), ...daily(index, day) })
    );
  return rows;
}

function reportCsv(rows: ReportRow[]): string {
  const quoted = (text: string) => `"${text.replace(/"/g, '""')}"`;
  const lines = [
    'Day,Ad ID,Ad name,Amount spent (USD),Impressions,Link clicks,Purchases,Purchases conversion value',
    ...rows.map((row) =>
      [
        row.day,
        row.adId,
        quoted(row.adName),
        row.spend.toFixed(2),
        1000,
        25,
        row.purchases,
        (row.purchases * 90).toFixed(2),
      ].join(',')
    ),
  ];
  return `${lines.join('\n')}\n`;
}

/** Spend and orders of one Meta ad over some rows. */
function totals(rows: ReportRow[], adId: string) {
  const mine = rows.filter((row) => row.adId === adId);
  return {
    spend: mine.reduce((sum, row) => sum + row.spend, 0),
    purchases: mine.reduce((sum, row) => sum + row.purchases, 0),
  };
}

async function expectNoInternalIds(page: Page) {
  await expect(page.locator('main')).not.toContainText(INTERNAL_ID);
}

/** The quote on screen is the one for the answers on screen. */
async function expectCurrentQuote(page: Page) {
  await expect(
    page.getByTestId('test-quote').getByText(en('tests', 'take-quote-stale'))
  ).toHaveCount(0, { timeout: 60_000 });
}

/** At phone width the page never scrolls sideways. */
async function expectFitsPhone(page: Page) {
  const before = page.viewportSize();
  await page.setViewportSize({ width: 375, height: 812 });
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth - window.innerWidth
      )
    )
    .toBeLessThanOrEqual(0);
  if (before) await page.setViewportSize(before);
}

/**
 * The three answers (the length keeps its default, 7 days), then the landing
 * page: the quote asks for it because the product, pasted as text, has no
 * link of its own.
 */
async function answer(page: Page, budget: number, { landing = true } = {}) {
  await page
    .getByLabel(en('tests', 'take-target-label'))
    .fill(String(TARGET_COST));
  await page.getByLabel(en('tests', 'take-budget-label')).fill(String(budget));
  if (!landing) return;
  const field = page.getByLabel(en('tests', 'take-landing-label'));
  await expect(field).toBeVisible({ timeout: 60_000 });
  await field.fill(LANDING_PAGE);
}

test('kept ads → take to test → package → full reports → matches → numbers, result, history, overview', async ({
  page,
}) => {
  test.setTimeout(900_000);
  const slug = provisioned.result.workspaceSlug;
  const files = mkdtempSync(path.join(tmpdir(), 'take-to-test-'));

  // Every quote the page reads, the latest last: what the page shows must be
  // what the server quoted.
  const quotes: any[] = [];
  page.on('response', async (response) => {
    if (
      new URL(response.url()).pathname.endsWith('/test-quote') &&
      response.ok()
    )
      quotes.push(await response.json().catch(() => null));
  });

  await test.step('the Owner signs up, enrolls MFA and the Workspace activates', async () => {
    await signUpOwner(page, provisioned, RUN_SUFFIX);
  });

  let ideasUrl = '';

  await test.step('step 1 in Controlled Mock: a pasted paragraph becomes ad ideas; two ads are kept', async () => {
    await useControlledMock(page);
    await page.goto(`/w/${slug}/start`);
    await page.getByLabel(en('ideas', 'start-paste-label')).fill(PRODUCT_TEXT);
    const start = page.getByRole('button', {
      name: en('ideas', 'start-button', { cost: '$0.00' }),
    });
    await expect(start).toBeEnabled({ timeout: 30_000 });
    await start.click();
    const stop = page.getByTestId('reading-stop');
    await expect(stop).toHaveAttribute('data-reason', 'several_models', {
      timeout: 180_000,
    });
    await stop.getByRole('radio').first().check();
    await stop
      .getByRole('button', { name: en('ideas', 'stop-several_models-action') })
      .click();
    await page.waitForURL(new RegExp(`/w/${slug}/p/[^/]+/ideas$`), {
      timeout: 600_000,
    });
    ideasUrl = page.url();
    const firstImage = page.getByTestId('ad-card').first().locator('img');
    await expect(firstImage).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText(en('ideas', 'ideas-ad-checking'))).toHaveCount(
      0,
      { timeout: 300_000 }
    );
    // One card at a time: each keep waits for the one before it.
    const cards = page.getByTestId('ad-card');
    for (const [index, card] of [cards.nth(0), cards.nth(1)].entries()) {
      await card
        .getByRole('button', { name: en('ideas', 'ideas-ad-keep') })
        .click();
      await expect(
        card.getByRole('button', { name: en('ideas', 'ideas-ad-kept') })
      ).toBeVisible({ timeout: 30_000 });
      await expect(page.getByTestId('ideas-kept-bar')).toContainText(
        en('ideas', 'ideas-kept-count', { count: index + 1 })
      );
    }
  });

  const productPath = () => new URL(ideasUrl).pathname.replace(/\/ideas$/, '');
  const takeUrl = () => `${productPath()}/take-to-test`;

  await test.step('kept ads not yet checked cannot be taken to test: the page sends the user to the check first', async () => {
    await page.getByTestId('ideas-take-to-test').click();
    await page.waitForURL(/\/take-to-test$/);
    await expect(
      page.getByRole('heading', {
        name: en('tests', 'take-title', { count: 2 }),
      })
    ).toBeVisible({ timeout: 60_000 });
    // Refused before anything else is asked.
    await answer(page, 1200, { landing: false });
    const unavailable = page.getByTestId('test-unavailable');
    await expect(unavailable).toContainText(
      en('tests', 'take-unavailable-ads_not_exportable', { count: 2 }),
      { timeout: 60_000 }
    );
    await expect(page.getByTestId('take-start')).toBeDisabled();
    await expectNoInternalIds(page);
    await unavailable
      .getByRole('link', { name: en('tests', 'take-go-check') })
      .click();
    await page.waitForURL(/\/ideas\?check=1$/);
  });

  const images: Download[] = [];

  await test.step('the one check before download (step 1); the two kept images download', async () => {
    const sheet = page.getByTestId('check-sheet');
    await expect(
      sheet.getByRole('heading', { name: en('ideas', 'check-title') })
    ).toBeVisible({ timeout: 60_000 });
    const unticked = sheet.locator('input[type="checkbox"]:not(:checked)');
    while ((await unticked.count()) > 0) await unticked.first().check();
    const open = sheet.getByRole('button', {
      name: en('ideas', 'check-finding-accept'),
      exact: true,
    });
    while ((await open.count()) > 0) await open.first().click();
    const confirm = page.getByTestId('check-confirm-bar').getByRole('button', {
      name: en('ideas', 'check-confirm', { count: 2 }),
    });
    await expect(confirm).toBeEnabled();
    page.on('download', (download) => images.push(download));
    await confirm.click();
    await expect.poll(() => images.length, { timeout: 60_000 }).toBe(2);
  });

  await test.step('three answers: the quote says a small budget gives only a rough read, what a verdict needs, and when the result comes', async () => {
    await page.goto(ideasUrl);
    await page.getByTestId('ideas-take-to-test').click();
    await page.waitForURL(/\/take-to-test$/);
    // Everything else is folded under the defaults.
    await expect(page.getByTestId('take-defaults')).not.toHaveAttribute(
      'open',
      /.*/
    );
    await expect(
      page.getByRole('heading', {
        name: en('tests', 'take-title', { count: 2 }),
      })
    ).toBeVisible({ timeout: 60_000 });
    quotes.length = 0;
    await answer(page, 240);
    const verdict = page.getByTestId('test-verdict');
    await expect(verdict).toContainText(en('tests', 'take-tier-rough'), {
      timeout: 60_000,
    });
    await expectCurrentQuote(page);
    // One sentence on what this round compares, from the quote.
    await expect(page.getByTestId('test-question')).toBeVisible();
    await expect.poll(() => quotes.at(-1)?.tier).toBe('rough');
    const quote = quotes.at(-1);
    // Controlled Mock: the test runs the way its ads were made.
    expect(quote.execution_mode).toBe('mock');
    // A verdict needs every compared object to reach the protocol's orders
    // at the target cost (design §5.6); the page shows the server's figure.
    expect(Number(quote.needed_budget)).toBe(
      quote.subjects.length * MIN_PURCHASES * TARGET_COST
    );
    await expect(page.getByTestId('test-needed')).toHaveText(
      en('tests', 'take-needed', { amount: usd(Number(quote.needed_budget)) })
    );
    await expect(page.getByTestId('test-quote')).toContainText(
      en('tests', 'take-conclusion', { date: dayText(quote.conclusion_date) })
    );
    await expectNoInternalIds(page);
    await shot(page, '10-take-to-test-rough');
    await expectFitsPhone(page);
  });

  await test.step('with enough budget the quote says so; the only cost confirmation is the start button', async () => {
    quotes.length = 0;
    await page.getByLabel(en('tests', 'take-budget-label')).fill('1200');
    await expect(page.getByTestId('test-verdict')).toContainText(
      en('tests', 'take-tier-enough'),
      { timeout: 60_000 }
    );
    await expectCurrentQuote(page);
    await expect.poll(() => quotes.at(-1)?.tier).toBe('enough');
    const quote = quotes.at(-1);
    expect(quote.available).toBe(true);
    // A 7-day test's result is due on day 16 (design §5.6, E9).
    const day16 = new Date(`${quote.start_date}T00:00:00Z`);
    day16.setUTCDate(day16.getUTCDate() + 15);
    expect(quote.conclusion_date).toBe(day16.toISOString().slice(0, 10));
    const start = page.getByTestId('take-start');
    await expect(start).toBeEnabled();
    // Controlled Mock checks cost nothing, so the button names no amount.
    expect(Number(quote.checks.maximum_cost)).toBe(0);
    await expect(start).toHaveText(en('tests', 'take-start-free'));
    await shot(page, '11-take-to-test');
  });

  let runUrl = '';

  await test.step('one click starts the test; the look-together checks run; their findings hold the package back until the user answers them', async () => {
    const started = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith('/tests') &&
        r.request().method() === 'POST'
    );
    await page.getByTestId('take-start').click();
    expect((await started).status()).toBe(201);
    const findings = page.getByTestId('test-open-findings');
    const pack = page.getByTestId('test-package');
    // Controlled Mock checks cannot tell, so every ad has open findings.
    await expect(findings).toBeVisible({ timeout: 300_000 });
    await expect(pack).toHaveCount(0);
    await expectNoInternalIds(page);
    await shot(page, '12-checks-findings', findings);
    const reasons = findings.getByLabel(en('tests', 'take-accept-label'));
    for (let i = 0; i < (await reasons.count()); i++)
      await reasons
        .nth(i)
        .fill('Looked at both ads side by side; each shows its own claim.');
    await findings
      .getByRole('button', { name: en('tests', 'take-accept') })
      .click();
    await expect(pack).toBeVisible({ timeout: 300_000 });
    await expect(pack).toContainText(
      en('tests', 'take-download-title', { count: 2 })
    );
    await expectNoInternalIds(page);
    await shot(page, '13-package-ready');
    await pack.getByTestId('test-open-run').click();
    await page.waitForURL(/\/tests\/[^/]+$/);
    runUrl = page.url();
    await expect(page.getByTestId('test-in-progress')).toBeVisible({
      timeout: 60_000,
    });
  });

  const runPath = () => new URL(runUrl).pathname;
  const overviewCard = () => page.getByTestId('overview-product').first();

  await test.step('the overview says the test is under way; taking the same product to test again opens this test', async () => {
    await page.goto(`/w/${slug}/overview`);
    await expect(overviewCard()).toHaveAttribute('data-step', 'testing', {
      timeout: 60_000,
    });
    await page.goto(takeUrl());
    await page.waitForURL(new RegExp(`${runPath()}$`), { timeout: 60_000 });
  });

  let adNames: string[] = [];

  await test.step('the ad package downloads; its ad names carry the package key', async () => {
    await expect(page.getByTestId('test-in-progress')).toBeVisible({
      timeout: 60_000,
    });
    await page.getByTestId('package-download').click();
    const open = page.getByRole('link', {
      name: en('tests', 'take-download-open'),
    });
    await expect(open).toBeVisible({ timeout: 60_000 });
    const archive = await page.request.get((await open.getAttribute('href'))!);
    expect(archive.status()).toBe(200);
    expect((await archive.body()).subarray(0, 2).toString()).toBe('PK');
    await page
      .getByText(en('tests', 'run-package-names'), { exact: true })
      .click();
    const prefix = en('tests', 'take-ad-name', { name: '' });
    const lines = await page
      .locator('li')
      .filter({ hasText: en('tests', 'take-ad-name', { name: 'MA-' }) })
      .allInnerTexts();
    adNames = lines.map((line) =>
      line
        .slice(line.indexOf(prefix) + prefix.length)
        .split('\n')[0]
        .trim()
    );
    expect(adNames).toHaveLength(2);
    for (const name of adNames) expect(name).toMatch(/^MA-[0-9a-f]{12} /);
    await expectNoInternalIds(page);
    await shot(page, '14-test-in-progress');
  });

  const testAds = () => [
    { adId: '3001', adName: adNames[0] },
    { adId: '3002', adName: adNames[1] },
  ];
  const allAds = () => [
    ...testAds(),
    { adId: OTHER_AD.id, adName: OTHER_AD.name },
  ];
  let secondReport: ReportRow[] = [];

  await test.step('the first full report: the short keys prefill two matches, the other ad waits; one confirmation; the start moves to the day the ads went live', async () => {
    const rows = reportRows(
      allAds(),
      5,
      (ad) =>
        [
          { spend: 40, purchases: 1 },
          { spend: 30, purchases: 0 },
          { spend: 20, purchases: 1 },
        ][ad]
    );
    const file = path.join(files, 'acme-charge-report-1.csv');
    writeFileSync(file, reportCsv(rows));
    await page.getByLabel(en('tests', 'run-report-file')).setInputFiles(file);
    await page.getByLabel(en('tests', 'run-report-account')).fill(AD_ACCOUNT);
    await page.getByTestId('report-upload-button').click();

    const bind = page.getByTestId('binding-confirm');
    await expect(bind).toBeVisible({ timeout: 60_000 });
    const rowOf = (name: string) =>
      bind.getByTestId('binding-row').filter({ hasText: name.slice(0, 15) });
    await expect(bind.getByTestId('binding-row')).toHaveCount(3);
    for (const name of adNames)
      await expect(rowOf(name)).toContainText(
        en('tests', 'run-bind-suggested-ma_key')
      );
    await expect(rowOf(OTHER_AD.name)).toContainText(
      en('tests', 'run-bind-status-no_match')
    );
    await expect(bind).toContainText(
      en('tests', 'run-bind-open_one', { count: 1 })
    );
    await expectNoInternalIds(page);
    await shot(page, '15-report-matches', bind);
    const confirmMatches = bind.getByTestId('binding-confirm-button');
    await expect(confirmMatches).toHaveText(
      en('tests', 'run-bind-confirm', { count: 2 })
    );
    await confirmMatches.click();

    const wentLive = dayText(isoDay(WENT_LIVE));
    const question = page.getByTestId('start-date-question');
    await expect(question).toContainText(
      en('tests', 'run-start-question', { day: wentLive }),
      { timeout: 30_000 }
    );
    await question
      .getByRole('button', {
        name: en('tests', 'run-start-question-move', { day: wentLive }),
      })
      .click();
    const confirmReport = page.getByTestId('report-confirm-button');
    await expect(confirmReport).toBeVisible({ timeout: 30_000 });
    await confirmReport.click();
    await expect(page.getByTestId('report-counted')).toBeVisible({
      timeout: 30_000,
    });
    // The ad outside the test is kept but not counted, and listed.
    const other = totals(rows, OTHER_AD.id);
    await expect(page.getByTestId('report-excluded')).toContainText(
      en('tests', 'run-report-excluded-other_ad', {
        id: OTHER_AD.id,
        count: 5,
        first: wentLive,
        last: dayText(isoDay(WENT_LIVE + 4)),
        spend: usd(other.spend),
      })
    );
  });

  await test.step('a second, longer full report replaces the days it repeats; the numbers are the later report only, and not a conclusion', async () => {
    await page
      .getByRole('button', { name: en('tests', 'run-report-another') })
      .click();
    secondReport = reportRows(
      allAds(),
      7,
      (ad, day) =>
        [
          { spend: 60, purchases: 2 },
          { spend: 50, purchases: day % 2 === 0 ? 1 : 0 },
          { spend: 20, purchases: 1 },
        ][ad]
    );
    const file = path.join(files, 'acme-charge-report-2.csv');
    writeFileSync(file, reportCsv(secondReport));
    await page.getByLabel(en('tests', 'run-report-file')).setInputFiles(file);
    await page.getByTestId('report-upload-button').click();
    const bind = page.getByTestId('binding-confirm');
    await expect(bind).toBeVisible({ timeout: 60_000 });
    await bind.getByTestId('binding-confirm-button').click();
    await page.getByTestId('report-confirm-button').click();
    await expect(page.getByTestId('report-counted')).toBeVisible({
      timeout: 30_000,
    });
    // The five days both reports hold, for every Meta ad in them (the one
    // outside the test too: it is kept, just not counted).
    await expect(page.getByTestId('report-counted')).toContainText(
      en('tests', 'run-report-replaces', { count: 15 })
    );

    const numbers = page.getByTestId('test-numbers');
    await expect(numbers).toContainText(en('tests', 'run-numbers-provisional'));
    const groups = numbers.getByTestId('numbers-comparison');
    const [a, b] = testAds().map((ad) => totals(secondReport, ad.adId));
    if ((await groups.count()) === 2) {
      for (const ad of [a, b]) {
        const group = groups.filter({ hasText: usd(ad.spend) });
        await expect(group).toHaveCount(1);
        await expect(group).toContainText(
          en('tests', 'run-numbers-short', {
            count: MIN_PURCHASES - ad.purchases,
            min: MIN_PURCHASES,
          })
        );
      }
    } else {
      await expect(groups).toHaveCount(1);
      await expect(groups).toContainText(usd(a.spend + b.spend));
      await expect(groups).toContainText(
        en('tests', 'run-numbers-short', {
          count: MIN_PURCHASES - a.purchases - b.purchases,
          min: MIN_PURCHASES,
        })
      );
    }
    await expectNoInternalIds(page);
    await shot(page, '16-numbers-so-far', numbers);
  });

  await test.step('the test history lists the test, its round and both reports with who sent them; the overview says a result waits', async () => {
    await page.goto(`${productPath()}/tests`);
    const item = page.getByTestId('history-test');
    await expect(item).toHaveAttribute('data-status', 'running', {
      timeout: 60_000,
    });
    await expect(item.getByTestId('history-round')).toHaveCount(1);
    const reports = item.getByTestId('history-report');
    await expect(reports).toHaveCount(2);
    await expect(reports.first()).toContainText(en('tests', 'history-who-you'));
    await expect(item.getByTestId('history-open-test')).toHaveAttribute(
      'href',
      runPath()
    );
    await expectNoInternalIds(page);
    await shot(page, '17-test-history');
    await expectFitsPhone(page);

    await page.goto(`/w/${slug}/overview`);
    await expect(overviewCard()).toHaveAttribute(
      'data-step',
      'conclusion_unseen',
      { timeout: 60_000 }
    );
    await expect(
      overviewCard().getByTestId('overview-product-action')
    ).toHaveAttribute('href', runPath());
    await expectNoInternalIds(page);
    await shot(page, '18-overview-result-waits');
  });

  const shortOf = (purchases: number) =>
    en('tests', 'run-result-short', { count: MIN_PURCHASES - purchases });

  await test.step('the result once the round is over: each group can’t tell yet and says how many orders it is short; the overview is back to the test under way', async () => {
    await overviewCard().getByTestId('overview-product-action').click();
    await page.waitForURL(new RegExp(`${runPath()}$`));
    await page.getByTestId('test-result').getByTestId('see-result').click();
    const result = page.getByTestId('result-comparisons');
    await expect(result).toBeVisible({ timeout: 30_000 });
    await expect(result).toContainText(en('tests', 'run-result-scope'));
    const [a, b] = testAds().map((ad) => totals(secondReport, ad.adId));
    if ((await result.locator('li').count()) === 2) {
      await expect(result).toContainText(shortOf(a.purchases));
      await expect(result).toContainText(shortOf(b.purchases));
    } else {
      await expect(result).toContainText(shortOf(a.purchases + b.purchases));
    }
    await expectNoInternalIds(page);
    await shot(page, '19-result', page.getByTestId('test-result'));
    await page.goto(`/w/${slug}/overview`);
    await expect(overviewCard()).toHaveAttribute('data-step', 'testing', {
      timeout: 60_000,
    });
  });

  await test.step('ending the round early: the minimal result counts only up to that day and says so', async () => {
    await page.goto(runUrl);
    await expect(page.getByTestId('test-schedule')).toBeVisible({
      timeout: 60_000,
    });
    const lastDay = isoDay(WENT_LIVE + 3);
    await page
      .getByTestId('test-schedule')
      .getByRole('button', { name: en('tests', 'run-schedule-end-early') })
      .click();
    const form = page.getByTestId('schedule-end-form');
    await form.getByLabel(en('tests', 'run-schedule-end-date')).fill(lastDay);
    await form
      .getByRole('button', { name: en('tests', 'run-schedule-end-save') })
      .click();
    await expect(form).toContainText(
      en('tests', 'run-schedule-end-warning', { day: dayText(lastDay) })
    );
    await form
      .getByRole('button', { name: en('tests', 'run-schedule-end-confirm') })
      .click();
    await expect(page.getByTestId('schedule-end-form')).toHaveCount(0, {
      timeout: 30_000,
    });
    await expect(page.getByTestId('test-schedule')).toContainText(
      en('tests', 'run-schedule-ended-early')
    );
    await page.getByTestId('test-result').getByTestId('see-result').click();
    const result = page.getByTestId('result-comparisons');
    await expect(result).toContainText(
      en('tests', 'run-result-ended-early', { day: dayText(lastDay) }),
      { timeout: 30_000 }
    );
    const upTo = secondReport.filter((row) => row.day <= lastDay);
    const [a, b] = testAds().map((ad) => totals(upTo, ad.adId));
    if ((await result.locator('li').count()) === 2) {
      await expect(result).toContainText(shortOf(a.purchases));
      await expect(result).toContainText(shortOf(b.purchases));
    } else {
      await expect(result).toContainText(shortOf(a.purchases + b.purchases));
    }
    await expect(result).toContainText(
      en('tests', 'run-result-status-not_clear')
    );
    await expectNoInternalIds(page);
    await expectFitsPhone(page);
  });

  const shots = process.env.E2E_ADS_FIRST_SHOTS;
  if (shots) {
    // For checking the same pages by hand afterwards: phone width, the other
    // language, dark mode. Kept with the screenshots, never in the repository.
    await page.context().storageState({ path: `${shots}/state.json` });
    writeFileSync(
      `${shots}/pages-step-2.json`,
      JSON.stringify(
        {
          slug,
          ideasUrl,
          takeUrl: takeUrl(),
          runUrl,
          historyUrl: `${productPath()}/tests`,
        },
        null,
        2
      )
    );
  }
});
