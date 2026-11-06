import { randomUUID } from 'node:crypto';
import { expect, type Page } from '@playwright/test';

/**
 * Calls the real domain API through the BFF as the signed-in member, for the
 * competitor specs that run against the compose stack (Controlled Mock: the
 * stack has no provider key, so nothing external is called).
 */
export const SLUG = process.env.E2E_WORKSPACE_SLUG ?? 'workspace-a';

export const PRODUCT_TEXT =
  'Acme Charge NACS to J1772 adapter. Lets J1772 electric vehicles charge at ' +
  'Halcyon Superchargers in North America. 48A, 250V, UL tested, weatherproof. ' +
  'Sold at acme-charge.example.';

export async function domain(
  page: Page,
  method: 'GET' | 'POST' | 'PUT' | 'PATCH',
  route: string,
  data?: unknown,
  headers: Record<string, string> = {}
) {
  const url = `/api/domain/v1/w/${SLUG}${route}`;
  if (method === 'GET') {
    const response = await page.request.get(url);
    expect(response.status(), `GET ${route}`).toBeLessThan(300);
    return response.json();
  }
  const { token } = await (await page.request.get('/api/csrf')).json();
  const response = await page.request.fetch(url, {
    method,
    data,
    headers: {
      Origin: new URL(page.url()).origin,
      'X-CSRF-Token': token,
      'X-Intent-Id': randomUUID(),
      ...headers,
    },
  });
  const body = await response.text();
  expect(response.status(), `${method} ${route}: ${body}`).toBeLessThan(300);
  return JSON.parse(body);
}

/** A confirmed product through the real paste-text intake (Controlled Mock). */
export async function createProduct(page: Page): Promise<string> {
  // A fresh product each run: the intake answers the same text with the
  // product it already made.
  const accepted = await domain(page, 'POST', '/products/intake', {
    text: `${PRODUCT_TEXT} Batch ${randomUUID().slice(0, 8)}.`,
  });
  let view: any;
  await expect
    .poll(
      async () => {
        view = await domain(
          page,
          'GET',
          `/products/intake/${accepted.intake_id}`
        );
        return view.status;
      },
      { timeout: 60_000, intervals: [1_000] }
    )
    .toMatch(/ready|failed|confirmed/);
  expect(view.status).not.toBe('failed');
  if (view.status === 'confirmed') return view.product_id;
  const variants = view.card?.model_variants ?? [];
  const confirmed = await domain(
    page,
    'POST',
    `/products/intake/${accepted.intake_id}/confirm`,
    {
      acknowledged_warnings: [],
      ...(variants.length > 1 ? { selected_variant: variants[0].label } : {}),
    }
  );
  return confirmed.product_id;
}

export const adLibrary = (pageId: string) =>
  `https://www.facebook.com/ads/library/?active_status=active&view_all_page_id=${pageId}`;

/** A competitor (or the product's own page) confirmed by its Ad Library link. */
export async function addCompetitor(
  page: Page,
  productId: string,
  pageId: string,
  role: 'competitor' | 'self' = 'competitor'
): Promise<string> {
  const created = await domain(page, 'POST', '/competitors', {
    product_id: productId,
    input: adLibrary(pageId),
    website: null,
    role,
  });
  expect(created.confirmation_status).toBe('confirmed');
  return created.competitor_id;
}

/** A Controlled Mock scan run to its end on the Worker; returns the scan. */
export async function runScan(
  page: Page,
  productId: string,
  competitorIds: string[],
  keywords: string[] = []
) {
  const accepted = await domain(page, 'POST', '/competitor-scans', {
    product_id: productId,
    competitor_ids: competitorIds,
    keywords,
    country: 'US',
    max_pages_per_competitor: 1,
    keyword_pages: 2,
    max_media_refreshes: 10,
    max_tagged_ads: 60,
    max_discover_advertisers: 30,
    execution_mode: 'mock',
  });
  let scan: any;
  await expect
    .poll(
      async () => {
        scan = await domain(
          page,
          'GET',
          `/competitor-scans/${accepted.scan_id}`
        );
        return scan.status;
      },
      { timeout: 180_000, intervals: [1_000] }
    )
    .toBe('completed');
  return scan;
}

/** The marketing project as the workspace reads it (plan and competitor review). */
export const PROJECT_READ =
  '?include_plan_review=true&include_competitor_review=true';

/**
 * One Controlled Mock planning call (understanding or plan), waited for;
 * returns the project read afterwards.
 */
export async function plan(
  page: Page,
  project: { id: string; revision: number },
  kind: 'understanding' | 'plan'
) {
  const call = await domain(
    page,
    'POST',
    `/marketing/projects/${project.id}/planning`,
    { kind, execution_mode: 'mock' },
    { 'If-Match': `"${project.revision}"` }
  );
  let read: any;
  await expect
    .poll(
      async () => {
        read = await domain(
          page,
          'GET',
          `/marketing/projects/${project.id}${PROJECT_READ}`
        );
        return read.latest_call?.id === call.id
          ? read.latest_call.state
          : 'queued';
      },
      { timeout: 120_000, intervals: [1_000] }
    )
    .not.toMatch(/^(queued|running)$/);
  expect(read.latest_call.error_code ?? null).toBeNull();
  return read;
}

/**
 * A real marketing project with its understanding confirmed (Controlled Mock
 * understanding; the member confirms each extracted fact, as the review card
 * does), which binds the project to its product anchor. That product has no
 * test plan, so a scan tags it against the expression mechanism catalog
 * alone (#329). Returns the project read afterwards.
 */
export async function confirmedProject(page: Page) {
  let project = await domain(
    page,
    'POST',
    `/marketing/projects${PROJECT_READ}`,
    {
      source_text: `${PRODUCT_TEXT} Batch ${randomUUID().slice(0, 8)}.`,
    }
  );
  project = await plan(page, project, 'understanding');
  project = await domain(
    page,
    'PATCH',
    `/marketing/projects/${project.id}${PROJECT_READ}`,
    {
      command: 'confirm_understanding',
      understanding: {
        ...project.understanding,
        facts: project.understanding.facts.map((fact: object) => ({
          ...fact,
          confirmation: 'confirmed',
        })),
      },
    },
    { 'If-Match': `"${project.revision}"` }
  );
  expect(project.product_id).toBeTruthy();
  return project;
}
