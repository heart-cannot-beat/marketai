import { test, expect, type Page } from '@playwright/test';
import {
  PROJECT_READ,
  SLUG,
  addCompetitor,
  confirmedProject,
  domain,
  plan,
  runScan,
} from './real-backend';
import { shot } from './shots';

/**
 * The board's drafting switch and new-snapshot hint (C-32 #275) against the
 * real drafting path of C-24 (#273): a real marketing project, Controlled
 * Mock understanding and plan drafts, real scans and the real read
 * projection — nothing is doubled.
 *
 * Needs the compose stack with its Worker, so it is opt-in:
 * `C32_REAL_BACKEND=1`.
 */
const HALCYON = '100000000010002';
const BRIGHTVOLT = '100000000000016';
const ACME = '100000000000010';

test.skip(
  !process.env.C32_REAL_BACKEND,
  'needs the compose stack with the C-21..C-24 Worker'
);

async function read(page: Page, projectId: string) {
  return domain(page, 'GET', `/marketing/projects/${projectId}${PROJECT_READ}`);
}

test('C-32 with C-24: the new-snapshot hint follows redrafts, and the switch keeps snapshots out of drafts', async ({
  page,
}) => {
  test.setTimeout(600_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/w/${SLUG}/experiments`);
  // A real project: Mock understanding, confirmed (which binds the project
  // to its product), then the product's competitors and a first scan.
  let project = await confirmedProject(page);
  const productId: string = project.product_id;
  const competitors = [
    await addCompetitor(page, productId, HALCYON),
    await addCompetitor(page, productId, BRIGHTVOLT),
    await addCompetitor(page, productId, ACME, 'self'),
  ];
  const first = await runScan(page, productId, competitors, ['NACS adapter']);
  expect(first.snapshot_id).toBeTruthy();

  // The first Mock plan draft cites the product's newest snapshot (US).
  project = await plan(page, project, 'plan');
  expect(project.competitor_review_support).toBe('v1');
  expect(project.plan.competitor_snapshot_id).toBe(first.snapshot_id);
  expect(project.competitor_snapshot_stale).toBe(false);

  await page.goto(`/w/${SLUG}/experiments?test=${project.id}`);
  const area = page.getByTestId('competitors-area');
  const toggleArea = area.locator('button[aria-controls="competitors-body"]');
  if ((await toggleArea.getAttribute('aria-expanded')) === 'false')
    await toggleArea.click();
  const summary = area.getByTestId('competitor-summary');
  await expect(
    summary.getByTestId('competitor-summary-cluster').first()
  ).toBeVisible();
  await expect(area.getByTestId('competitor-snapshot-stale')).toHaveCount(0);

  // A new scan of the same product and market: the cited snapshot is older.
  const second = await runScan(page, productId, competitors, ['NACS adapter']);
  expect(second.snapshot_id).not.toBe(first.snapshot_id);
  expect((await read(page, project.id)).competitor_snapshot_stale).toBe(true);
  await page.reload();
  if ((await toggleArea.getAttribute('aria-expanded')) === 'false')
    await toggleArea.click();
  const hint = area.getByTestId('competitor-snapshot-stale');
  await expect(hint).toHaveText(
    'A newer competitor snapshot is available; redraft to use it.'
  );
  await expect(
    summary.getByTestId('competitor-summary-cluster').first()
  ).toBeVisible();
  await expect(area.getByTestId('competitor-drafting-toggle')).toHaveAttribute(
    'aria-checked',
    'true'
  );
  await shot(page, 'c32-12-real-stale-hint', hint);

  // Redrafting cites the newer snapshot; the hint goes away.
  project = await plan(page, await read(page, project.id), 'plan');
  expect(project.plan.competitor_snapshot_id).toBe(second.snapshot_id);
  expect(project.competitor_snapshot_stale).toBe(false);
  await page.reload();
  if ((await toggleArea.getAttribute('aria-expanded')) === 'false')
    await toggleArea.click();
  await expect(
    summary.getByTestId('competitor-summary-cluster').first()
  ).toBeVisible();
  await expect(area.getByTestId('competitor-snapshot-stale')).toHaveCount(0);

  // Switched off on the board: the next draft cites no snapshot.
  const toggle = area.getByTestId('competitor-drafting-toggle');
  await expect(toggle).toHaveAttribute('aria-checked', 'true');
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-checked', 'false');
  project = await plan(page, await read(page, project.id), 'plan');
  expect(project.plan.competitor_snapshot_id ?? null).toBeNull();
  expect(
    (project.plan.angles ?? []).flatMap(
      (angle: { competitor_overlap?: string[] }) =>
        angle.competitor_overlap ?? []
    )
  ).toEqual([]);
  await shot(
    page,
    'c32-13-real-drafting-off',
    area.getByTestId('competitor-drafting-switch')
  );
});
