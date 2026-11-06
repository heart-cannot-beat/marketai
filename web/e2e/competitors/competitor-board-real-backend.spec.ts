import {
  test,
  expect,
  type ConsoleMessage,
  type Locator,
  type Page,
  type Response,
} from '@playwright/test';
import { CompetitorDouble, PROJECT_ID } from './double';
import { SLUG, adLibrary, confirmedProject, domain } from './real-backend';
import { shot } from './shots';

/**
 * The competitor board (C-32 #275) on the real C-21 / C-22 / C-23 backend in
 * Controlled Mock, from the C-31 panel's scan on: the stack has no provider
 * key, so the fetch, the filter, tag and cluster steps and the recluster are
 * all the Mock (nothing external is called). The product is a real marketing
 * project's (Mock understanding, confirmed): it has no test plan, so the tag
 * step tags against the expression mechanism catalog every product has
 * (#329). Only the marketing project the page opens is doubled, including
 * C-24's `competitor_snapshot_stale` (its real path is the drafting spec's).
 *
 * Needs the compose stack with its Worker, so it is opt-in:
 * `C32_REAL_BACKEND=1`.
 */
const ACME = '100000000000010';
const HALCYON = '100000000010002';
const BRIGHTVOLT = '100000000000016';

test.skip(
  !process.env.C32_REAL_BACKEND,
  'needs the compose stack with the C-21..C-23 Worker'
);

type Summary = {
  snapshot_id: string;
  counts: Record<string, number>;
  relevance_sources: Record<string, number>;
  clusters: { cluster_id: string; claim: string; ad_ids: string[] }[];
  unclustered: { ad_id: string }[];
  dimension_counts: { label: string; values: { confirmed: number }[] }[];
};

const confirmedTags = (summary: Summary) =>
  summary.dimension_counts
    .flatMap((factor) => factor.values)
    .reduce((sum, value) => sum + value.confirmed, 0);

/** Nothing on a card at 390 px is cut off: the source line least of all. */
async function expectUnclipped(locator: Locator) {
  const box = await locator.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    return {
      overflow: node.scrollWidth - node.clientWidth,
      right: rect.right,
      width: window.innerWidth,
    };
  });
  expect(box.overflow).toBeLessThanOrEqual(0);
  expect(box.right).toBeLessThanOrEqual(box.width);
}

async function addByLink(area: Locator, pageId: string, role: string) {
  await area
    .getByLabel('Page link, Ad Library link or name')
    .fill(adLibrary(pageId));
  await area.getByLabel('Role').selectOption(role);
  await area.getByRole('button', { name: 'Add', exact: true }).click();
  await expect(
    area.getByTestId('competitor-row').filter({ hasText: pageId })
  ).toHaveAttribute('data-confirmation', 'confirmed');
}

async function summaryOf(page: Page, scanId: string): Promise<Summary> {
  return domain(page, 'GET', `/competitor-scans/${scanId}/summary`);
}

test('C-32 board on the real backend: scan, flip, tags, refreeze, recluster, resume and the drafting switch', async ({
  page,
  browser,
}) => {
  test.setTimeout(900_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/w/${SLUG}/experiments`);
  // A marketing project's product: no test plan (it cannot have one), yet
  // the scan freezes a vocabulary for the tag step: the expression catalog.
  const productId: string = (await confirmedProject(page)).product_id;
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
  await expect(area.getByText(/No competitors yet/)).toBeVisible();

  // Two competitors and the product's own page, each confirmed by its link.
  await addByLink(area, HALCYON, 'competitor');
  await addByLink(area, BRIGHTVOLT, 'competitor');
  await addByLink(area, ACME, 'self');

  // The scan starts from the C-31 panel, on the real C-21 Worker (Mock).
  const setup = area.getByTestId('competitor-scan-setup');
  await setup.getByLabel('Keywords (optional)').fill('NACS adapter');
  await setup.getByLabel('Visual tagging limit').fill('150');
  await expect(setup.getByTestId('competitor-scan-quote')).toHaveAttribute(
    'data-mode',
    'mock'
  );
  await setup.getByTestId('competitor-scan-start').click();
  const progress = area.getByTestId('competitor-scan-progress');
  await expect(progress).toHaveAttribute('data-status', 'completed', {
    timeout: 300_000,
  });
  for (const step of ['filter', 'tag', 'cluster', 'freeze'])
    await expect(
      progress.getByTestId(`competitor-scan-step-${step}`)
    ).toHaveAttribute('data-status', 'completed');
  await expect
    .poll(() => new URL(page.url()).searchParams.get('competitor_scan'))
    .toBeTruthy();
  const scanId = new URL(page.url()).searchParams.get('competitor_scan')!;
  // The frozen vocabulary is the catalog's one factor, labelled in English.
  const frozen = await summaryOf(page, scanId);
  expect(frozen.dimension_counts.map((factor) => factor.label)).toEqual([
    'Expression mechanism',
  ]);

  // The board: every card's thumbnail is the archived derivative with its
  // source and the original link under it.
  const board = area.getByTestId('competitor-board');
  const summary = area.getByTestId('competitor-summary');
  await expect(
    summary.getByTestId('competitor-summary-cluster').first()
  ).toBeVisible();
  const first = board.getByTestId('competitor-ad-card').first();
  await expect(first.getByTestId('competitor-ad-thumbnail')).toHaveAttribute(
    'src',
    /^blob:/
  );
  const source = first.getByTestId('competitor-ad-source');
  await expect(source).toHaveText(
    'Source: Meta Ad Library · View the original ad'
  );
  await expect(
    source.getByRole('link', { name: 'View the original ad' })
  ).toHaveAttribute(
    'href',
    /^https:\/\/www\.facebook\.com\/ads\/library\/?\?id=\d+/
  );
  await expectUnclipped(source);
  await expectUnclipped(first);
  expect(await page.locator('a[download]').count()).toBe(0);
  await shot(page, 'c32-01-board-card', first);
  // The product's own page is a group of its own (scenario 14).
  const own = board.getByTestId('competitor-board-self');
  await expect(own.getByTestId('competitor-ad-card').first()).toHaveAttribute(
    'data-role',
    'self'
  );
  expect(
    await board
      .getByTestId('competitor-board-main')
      .locator('[data-role="self"]')
      .count()
  ).toBe(0);
  await shot(page, 'c32-02-board-self-group', own);
  const before = await summaryOf(page, scanId);
  await shot(page, 'c32-03-summary', summary);

  // Scenario 7: Halcyon Motors' self-driving ad is unrelated; flip one to relevant.
  await board
    .getByTestId('competitor-board-filter-relevance')
    .selectOption('unrelated');
  const fsd = board
    .getByTestId('competitor-ad-card')
    .filter({ hasText: 'Explore Full Self-Driving' })
    .first();
  await expect(fsd).toHaveAttribute('data-relevance', 'unrelated');
  await expect(fsd.getByTestId('competitor-judgment-source')).toHaveText(
    'AI inference'
  );
  const fsdId = (await fsd.getAttribute('data-ad-id'))!;
  await fsd.getByTestId('competitor-ad-flip-relevant').click();
  await expect(summary.getByTestId('competitor-summary-pending')).toBeVisible();
  // Halcyon Motors' relevant ads are now only the flipped one.
  await board
    .getByTestId('competitor-board-filter-relevance')
    .selectOption('relevant');
  // A page confirmed by its link is named from its ads since #332.
  await board
    .getByTestId('competitor-board-filter-competitor')
    .selectOption({ label: 'Halcyon Motors' });
  const flipped = board.locator(`[data-ad-id="${fsdId}"]`);
  await expect(board.getByTestId('competitor-ad-card')).toHaveCount(1);
  await expect(flipped).toHaveAttribute('data-relevance', 'relevant');
  await expect(flipped).toHaveAttribute('data-relevance-source', 'user');
  await expect(flipped.getByTestId('competitor-judgment-source')).toHaveText(
    "Member's judgment"
  );
  await flipped.getByTestId('competitor-ad-history-toggle').click();
  const history = flipped.getByTestId('competitor-ad-history-entry');
  await expect(history).toHaveCount(2);
  await expect(history.first()).toHaveAttribute('data-source', 'user');
  await expect(history.last()).toHaveAttribute('data-source', 'ai');
  await shot(page, 'c32-04-flip-history', flipped);

  // Refreeze (free): the flipped ad lands in "Not clustered" (next to the
  // relevant ads no Mock claim matched in the first place).
  await summary.getByTestId('competitor-refreeze').click();
  await expect(
    summary.getByTestId('competitor-refreeze-result')
  ).toHaveAttribute('data-created', 'true');
  const unclustered = summary.getByTestId('competitor-summary-unclustered');
  expect(before.unclustered.map((item) => item.ad_id)).not.toContain(fsdId);
  await expect(unclustered).toHaveAttribute(
    'data-count',
    String(before.unclustered.length + 1)
  );
  const showAll = unclustered.getByTestId('competitor-summary-ads-toggle');
  if (await showAll.isVisible()) await showAll.click();
  await expect(unclustered.locator(`[data-ad-id="${fsdId}"]`)).toBeVisible();
  const refrozen = await summaryOf(page, scanId);
  expect(refrozen.snapshot_id).not.toBe(before.snapshot_id);
  expect(refrozen.counts.relevant).toBe(before.counts.relevant + 1);
  expect(refrozen.counts.unrelated).toBe(before.counts.unrelated - 1);
  expect(refrozen.relevance_sources.non_ai).toBe(
    before.relevance_sources.non_ai + 1
  );
  await expect(
    summary.getByTestId('competitor-summary-count-relevant')
  ).toHaveText(String(refrozen.counts.relevant));
  await shot(page, 'c32-05-unclustered', unclustered);
  // The same revisions freeze to the same snapshot.
  await summary.getByTestId('competitor-refreeze').click();
  await expect(
    summary.getByTestId('competitor-refreeze-result')
  ).toHaveAttribute('data-created', 'false');

  // Confirm one AI tag suggestion as a member's revision.
  await board
    .getByTestId('competitor-board-filter-competitor')
    .selectOption('all');
  const suggested = board
    .getByTestId('competitor-ad-card')
    .filter({
      has: page.getByTestId('competitor-ad-tags-state').filter({
        hasText: 'AI suggestion',
      }),
    })
    .first();
  const taggedId = (await suggested.getAttribute('data-ad-id'))!;
  await suggested.getByTestId('competitor-ad-tags-edit').click();
  const editor = suggested.getByTestId('competitor-tag-editor');
  // The scan's vocabulary is the catalog's one factor (#329); wait for it
  // before reading the options.
  await expect(
    editor.getByText('Expression mechanism', { exact: true })
  ).toBeVisible();
  const factor = editor.locator('select').first();
  await expect(
    factor.getByRole('option', { name: 'Route visualization' })
  ).toBeAttached();
  const options = await factor
    .locator('option')
    .evaluateAll((nodes) =>
      nodes.map((node) => (node as HTMLOptionElement).value)
    );
  const currentValue = await factor.inputValue();
  await factor.selectOption(
    options.find((value) => value && value !== currentValue) ?? ''
  );
  await editor.getByTestId('competitor-tag-confirm').click();
  const tagged = board.locator(`[data-ad-id="${taggedId}"]`);
  await expect(tagged.getByTestId('competitor-ad-tags-state')).toHaveText(
    'Confirmed'
  );
  await expect(tagged.getByTestId('competitor-ad-tags')).toContainText(
    'Expression mechanism'
  );
  await tagged.getByTestId('competitor-ad-tags-edit').click();
  const tagHistory = tagged.getByTestId('competitor-tag-history-entry');
  await expect(tagHistory).toHaveCount(2);
  await expect(tagHistory.first()).toHaveAttribute('data-source', 'user');
  await shot(page, 'c32-06-tags-confirmed', tagged);
  await summary.getByTestId('competitor-refreeze').click();
  await expect(
    summary.getByTestId('competitor-refreeze-result')
  ).toHaveAttribute('data-created', 'true');
  const confirmedNow = await summaryOf(page, scanId);
  // Each factor the member's revision gives a value counts as confirmed.
  const revision = await domain(
    page,
    'GET',
    `/competitor-scans/${scanId}/ads/${taggedId}/tags`
  );
  expect(revision.current.source).toBe('user');
  const valued = revision.current.factors.filter(
    (item: { value_id: string | null }) => item.value_id
  ).length;
  expect(valued).toBeGreaterThan(0);
  expect(confirmedTags(confirmedNow)).toBe(confirmedTags(refrozen) + valued);

  // Recluster (paid): the quote first, then the run, then new clusters.
  await summary.getByTestId('competitor-recluster').click();
  const quote = summary.getByTestId('competitor-recluster-quote');
  await expect(quote).toHaveAttribute('data-mode', 'mock');
  await expect(
    quote.getByTestId('competitor-recluster-quote-total')
  ).toContainText('no charge');
  await shot(page, 'c32-07-recluster-quote', quote);
  // The flipped ad left a resume on offer (production S1, #355). Confirming
  // asks the backend nothing it refuses while the recluster runs: neither
  // quote again, so no 4xx and no console error.
  await expect(area.getByTestId('competitor-scan-resume')).toBeVisible();
  const refused: string[] = [];
  const consoleErrors: string[] = [];
  const onResponse = (response: Response) => {
    if (response.url().includes('/api/domain/') && response.status() >= 400)
      refused.push(
        `${response.status()} ${response.request().method()} ${new URL(response.url()).pathname}`
      );
  };
  const onConsole = (message: ConsoleMessage) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  };
  page.on('response', onResponse);
  page.on('console', onConsole);
  await quote.getByTestId('competitor-recluster-confirm').click();
  await expect(
    summary.getByTestId('competitor-recluster-status')
  ).toHaveAttribute('data-status', 'completed', { timeout: 120_000 });
  page.off('response', onResponse);
  page.off('console', onConsole);
  expect(refused).toEqual([]);
  expect(consoleErrors).toEqual([]);
  await expect(unclustered).toHaveAttribute(
    'data-count',
    String(before.unclustered.length),
    { timeout: 30_000 }
  );
  await expect(unclustered.locator(`[data-ad-id="${fsdId}"]`)).toHaveCount(0);
  const reclustered = await summaryOf(page, scanId);
  expect(reclustered.snapshot_id).not.toBe(confirmedNow.snapshot_id);
  expect(reclustered.clusters).toHaveLength(before.clusters.length + 1);
  const home = reclustered.clusters.find((cluster) =>
    cluster.ad_ids.includes(fsdId)
  );
  // The Mock's claim for self-driving copy: a cluster that did not exist.
  expect(home?.claim).toMatch(/Let the car do the driving/);
  expect(before.clusters.map((cluster) => cluster.claim)).not.toContain(
    home!.claim
  );
  await expect(
    summary
      .getByTestId('competitor-summary-cluster')
      .filter({ hasText: home!.claim })
  ).toBeVisible();
  await shot(
    page,
    'c32-08-summary-reclustered',
    summary.getByTestId('competitor-summary-cluster').first()
  );

  // The flipped ad was never tagged, so the C-31 panel offers a resume
  // (real C-21/C-23 backend: an LLM-only resume, quoted first).
  await page.reload();
  const resume = area.getByTestId('competitor-scan-resume');
  await expect(resume.getByTestId('competitor-scan-quote')).toHaveAttribute(
    'data-kind',
    'resume'
  );
  await shot(page, 'c32-09-resume-offer', resume);
  await resume.getByTestId('competitor-scan-resume-button').click();
  await expect(area.getByText(/Resumed 1 time/)).toBeVisible();
  // The resume's own Job tags the flipped ad; the scan stays one scan.
  await expect
    .poll(
      async () =>
        (
          await domain(
            page,
            'GET',
            `/competitor-scans/${scanId}/ads/${fsdId}/tags`
          )
        ).current?.source ?? null,
      { timeout: 180_000, intervals: [2_000] }
    )
    .toBe('ai');
  await expect(progress).toHaveAttribute('data-status', 'completed', {
    timeout: 60_000,
  });
  await expect(resume).toHaveCount(0);
  const resumed = await domain(page, 'GET', `/competitor-scans/${scanId}`);
  expect(resumed.scan_id).toBe(scanId);
  expect(resumed.resumes).toHaveLength(1);
  expect(resumed.can_resume).toBe(false);

  // The drafting switch reads back off after a reload.
  const toggle = area.getByTestId('competitor-drafting-toggle');
  await expect(toggle).toHaveAttribute('aria-checked', 'true');
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-checked', 'false');
  await page.reload();
  await expect(area.getByTestId('competitor-drafting-toggle')).toHaveAttribute(
    'aria-checked',
    'false'
  );
  const settings = await domain(
    page,
    'GET',
    `/products/${productId}/competitor-settings`
  );
  expect(settings.competitor_snapshot_in_drafts).toBe(false);
  await shot(
    page,
    'c32-10-drafting-off',
    area.getByTestId('competitor-drafting-switch')
  );

  // The new-snapshot hint follows the projection's flag (doubled until
  // C-24 #273 ships it); a projection without it shows nothing.
  await expect(area.getByTestId('competitor-snapshot-stale')).toHaveCount(0);
  double.project = { competitor_snapshot_stale: true };
  await page.reload();
  const hint = area.getByTestId('competitor-snapshot-stale');
  await expect(hint).toHaveText(
    'A newer competitor snapshot is available; redraft to use it.'
  );
  await shot(page, 'c32-11-stale-hint', hint);
  double.project = { competitor_snapshot_stale: false };
  await page.reload();
  await expect(area.getByTestId('competitor-summary')).toBeVisible();
  await expect(area.getByTestId('competitor-snapshot-stale')).toHaveCount(0);

  // zh-CN: the same board in Chinese, no English left over.
  const zh = await browser.newContext({
    locale: 'zh-CN',
    viewport: { width: 390, height: 844 },
    storageState: 'e2e/.auth/owner.json',
  });
  const zhPage = await zh.newPage();
  await double.install(zhPage);
  await zhPage.goto(
    `/zh-CN/w/${SLUG}/experiments?test=${PROJECT_ID}&competitor_scan=${scanId}`
  );
  const zhArea = zhPage.getByTestId('competitors-area');
  const zhSummary = zhArea.getByTestId('competitor-summary');
  await expect(zhSummary.getByText('汇总', { exact: true })).toBeVisible();
  await expect(zhSummary).toContainText('起草时使用竞品快照');
  await expect(zhSummary).toContainText('AI 推测');
  const zhCard = zhArea.getByTestId('competitor-ad-card').first();
  await expect(zhCard.getByTestId('competitor-ad-source')).toHaveText(
    '来源：Meta Ad Library · 查看原始广告'
  );
  await expectUnclipped(zhCard.getByTestId('competitor-ad-source'));
  const zhBoard = zhArea.getByTestId('competitor-board-area');
  for (const english of [
    'Source: Meta Ad Library',
    'View the original ad',
    'Summary',
    'Buying claims',
    'Not clustered',
    'Refreeze',
    'Recluster',
    'AI inference',
    'Change to',
    'Use the competitor snapshot',
  ])
    await expect(zhBoard).not.toContainText(english);
  await shot(zhPage, 'c32-zh-01-summary', zhSummary);
  await shot(zhPage, 'c32-zh-02-card', zhCard);

  // Scenario 15: a scan started in zh-CN judges and clusters in Chinese
  // (the language is frozen with the scan; the one above stays English).
  await zhArea.getByRole('button', { name: '新扫描' }).click();
  const zhSetup = zhArea.getByTestId('competitor-scan-setup');
  await zhSetup.getByLabel('关键词（可选）').fill('NACS adapter');
  await zhSetup.getByTestId('competitor-scan-start').click();
  await expect
    .poll(
      () => new URL(zhPage.url()).searchParams.get('competitor_scan') ?? scanId,
      { timeout: 30_000 }
    )
    .not.toBe(scanId);
  await expect(zhArea.getByTestId('competitor-scan-progress')).toHaveAttribute(
    'data-status',
    'completed',
    { timeout: 300_000 }
  );
  const zhConclusion = zhArea.getByTestId('competitor-summary-conclusion');
  await expect(zhConclusion).toContainText('受控 Mock，未调用模型');
  await expect(
    zhArea.getByTestId('competitor-summary-cluster').first()
  ).toContainText('（受控 Mock）');
  const zhRelevant = zhArea
    .getByTestId('competitor-ad-card')
    .filter({
      has: zhPage.getByTestId('competitor-judgment-source').filter({
        hasText: 'AI 推测',
      }),
    })
    .first();
  await expect(zhRelevant).toContainText('受控 Mock');
  // Its frozen vocabulary names the catalog in Chinese (#329).
  const zhTagged = zhArea
    .getByTestId('competitor-ad-tags')
    .filter({ hasText: '表达机制' })
    .first();
  await expect(zhTagged).toBeVisible();
  await shot(zhPage, 'c32-zh-03-zh-scan-summary', zhConclusion);
  await shot(zhPage, 'c32-zh-04-zh-scan-tags', zhTagged);
  await zh.close();
});
