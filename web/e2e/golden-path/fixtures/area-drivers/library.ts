import { expect, type Page } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import type {
  BrandedOpaqueId,
  EntityRef,
  LibraryClaimCode,
  LibraryEvidence,
} from '../../artifacts/schemas';
import type { OwnerSessionCapability } from '../onboarding-harness';

export interface LibrarySetupConfig {
  workspaceSlug: string;
  productName: string;
  offerName: string;
  personaName: string;
  templateName: string;
  promptPolicyName: string;
  dataSourceName: string;
  referenceImagePath: string;
  markets: string[];
}

type JsonObject = Record<string, unknown>;

interface LibraryEntryResponse extends JsonObject {
  id: string;
  revision?: number;
  draft?: { revision?: number };
}

interface ImpactResponse extends JsonObject {
  impact_id: string;
  impact_hash: string;
  target_revision?: number | null;
}

function entityRef<K extends string>(
  kind: K,
  runtimeId: string,
  observedVia: 'dom' | 'same_origin_response'
): EntityRef<K & import('../../artifacts/schemas').EntityKind> {
  return {
    kind: kind as K & import('../../artifacts/schemas').EntityKind,
    runtimeId: runtimeId as BrandedOpaqueId<
      K & import('../../artifacts/schemas').EntityKind
    >,
    observedVia,
  };
}

/**
 * Make an authenticated same-origin command while preserving the exact
 * browser boundary used by the Library UI (CSRF, intent and conditional
 * revision headers). The UI's Draft tab is intentionally read-only in this
 * build, so the driver submits the same command contract directly after
 * navigating the visible Library surface.
 */
async function command<T extends JsonObject>(
  page: Page,
  path: string,
  method: 'POST' | 'PUT',
  body?: JsonObject,
  options: { ifMatch?: number } = {}
): Promise<T> {
  const intentId = randomUUID();
  const traceId = `golden-path-${randomUUID()}`;
  const result = await page.evaluate(
    async ({
      path: requestPath,
      method: requestMethod,
      body: requestBody,
      ifMatch,
      intentId,
      traceId,
    }) => {
      const csrfResponse = await fetch('/api/csrf', {
        credentials: 'same-origin',
      });
      if (!csrfResponse.ok) {
        throw new Error(`CSRF token request failed: ${csrfResponse.status}`);
      }
      const csrf = (await csrfResponse.json()) as { token?: string };
      if (!csrf.token) throw new Error('CSRF response did not contain a token');

      const headers: Record<string, string> = {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrf.token,
        'X-Intent-Id': intentId,
        'X-Trace-Id': traceId,
      };
      if (ifMatch !== undefined) headers['If-Match'] = String(ifMatch);

      const response = await fetch(requestPath, {
        method: requestMethod,
        credentials: 'same-origin',
        headers,
        body:
          requestBody === undefined ? undefined : JSON.stringify(requestBody),
      });
      const text = await response.text();
      let parsed: unknown = {};
      try {
        parsed = text ? JSON.parse(text) : {};
      } catch {
        parsed = { detail: text };
      }
      return { status: response.status, body: parsed };
    },
    {
      path,
      method,
      body,
      ifMatch: options.ifMatch,
      intentId,
      traceId,
    }
  );

  if (result.status < 200 || result.status >= 300) {
    throw new Error(
      `Library command ${method} ${path} failed (${result.status}): ${JSON.stringify(result.body)}`
    );
  }
  return result.body as T;
}

async function createRootThroughUi(
  page: Page,
  workspaceSlug: string,
  categoryLabel: string,
  displayName: string,
  content: JsonObject
): Promise<LibraryEntryResponse> {
  await page.goto(`/w/${workspaceSlug}/library`);
  await page
    .locator('aside')
    .getByRole('button', { name: categoryLabel, exact: true })
    .click();
  const newEntryButton = page
    .getByRole('button', { name: 'New entry', exact: true })
    .first();
  await expect(newEntryButton).toBeEnabled();
  await newEntryButton.click();
  const dialog = page.getByRole('dialog').last();
  await expect(dialog).toBeVisible();
  await dialog.getByRole('textbox').fill(displayName);
  const createButton = dialog.getByRole('button', {
    name: 'Create',
    exact: true,
  });
  await expect(createButton).toBeEnabled();
  const responsePromise = page.waitForResponse(
    (response) =>
      response.url().includes('/library/entries') &&
      response.request().method() === 'POST' &&
      response.status() === 201
  );
  await createButton.click();
  const response = await responsePromise;
  const entry = (await response.json()) as LibraryEntryResponse;
  await expect(
    page.getByRole('heading', { name: displayName, exact: true })
  ).toBeVisible();
  await saveDraft(page, workspaceSlug, entry, content);
  return entry;
}

async function createInternalThroughUi(
  page: Page,
  workspaceSlug: string,
  parentEntryId: string,
  displayName: string,
  content: JsonObject
): Promise<LibraryEntryResponse> {
  const responsePromise = page.waitForResponse(
    (response) =>
      response
        .url()
        .includes(
          `/library/entries/${encodeURIComponent(parentEntryId)}/internal`
        ) &&
      response.url().includes('/internal') &&
      response.request().method() === 'POST' &&
      response.status() === 201
  );
  await page.getByRole('button', { name: /New Offer/i }).click();
  const dialog = page.getByRole('dialog').last();
  await dialog.getByRole('textbox').fill(displayName);
  await dialog.getByRole('button', { name: 'Create', exact: true }).click();
  const response = await responsePromise;
  const entry = (await response.json()) as LibraryEntryResponse;
  await expect(
    page.getByRole('heading', { name: displayName, exact: true })
  ).toBeVisible();
  await saveDraft(page, workspaceSlug, entry, content);
  return entry;
}

async function saveDraft(
  page: Page,
  workspaceSlug: string,
  entry: LibraryEntryResponse,
  content: JsonObject
): Promise<void> {
  const saved = await command<JsonObject>(
    page,
    `/api/domain/v1/w/${encodeURIComponent(workspaceSlug)}/library/entries/${encodeURIComponent(entry.id)}/draft`,
    'PUT',
    { content },
    { ifMatch: Number(entry.draft?.revision ?? 1) }
  );
  if (typeof saved.revision === 'number') {
    entry.revision = saved.revision;
    entry.draft = { revision: saved.revision };
  }
}

async function publishEntry(
  page: Page,
  workspaceSlug: string,
  entry: LibraryEntryResponse
): Promise<JsonObject> {
  const slug = encodeURIComponent(workspaceSlug);
  const impact = await command<ImpactResponse>(
    page,
    `/api/domain/v1/w/${slug}/library/entries/${encodeURIComponent(entry.id)}/publish-impact`,
    'POST'
  );
  const targetRevision = Number(
    impact.target_revision ?? entry.draft?.revision ?? 1
  );

  // MFA is checked only at login (ADR 0015): publishing needs no grant.
  return command<JsonObject>(
    page,
    `/api/domain/v1/w/${slug}/library/entries/${encodeURIComponent(entry.id)}/publish`,
    'POST',
    {
      impact_id: impact.impact_id,
      impact_hash: impact.impact_hash,
      acknowledged_warnings: [],
    },
    { ifMatch: targetRevision }
  );
}

function productContent(config: LibrarySetupConfig): JsonObject {
  return {
    name: config.productName,
    description: 'Golden Path product for deterministic generation coverage.',
    audience: 'Golden Path operators',
    selling_points: ['Reliable', 'Traceable'],
    must_match: [
      {
        id: 'golden-path-product-visible',
        type: 'visual',
        description: 'The product remains clearly visible.',
        severity: 'blocking',
        viewpoints: ['front'],
        qc_method: 'human_review',
        confirmed: true,
      },
    ],
  };
}

function templateContent(config: LibrarySetupConfig): JsonObject {
  return {
    display_name: config.templateName,
    dimensions: [
      {
        id: 'market',
        name: 'Market',
        values: ['us', 'de', 'ca', 'jp', 'gb'].map((id) => ({
          id,
          name: id.toUpperCase(),
          positive_fragments: [`${id.toUpperCase()} market`],
          negative_fragments: [],
        })),
      },
    ],
    constraints: [],
    concept_key_mappings: [],
    combination_cardinality: 'one_per_dimension',
  };
}

function promptPolicyContent(): JsonObject {
  return {
    registered_tokens: [],
    locked_phrases: [],
    brand_tone: { style: 'neutral' },
    allowed_template_entry_ids: [],
    optional_clauses: [],
  };
}

function dataSourceContent(markets: string[]): JsonObject {
  return {
    adapter_kind: 'manual',
    execution_mode: 'mock',
    config: { markets },
  };
}

export class LibraryDriver {
  async setup(
    session: OwnerSessionCapability,
    config: LibrarySetupConfig
  ): Promise<LibraryEvidence> {
    const page = session.page();
    const base = `/w/${config.workspaceSlug}`;
    await page.goto(`${base}/library`);
    await page.waitForSelector('[data-testid="library-shell"]');
    const categoryNav = page.locator('aside').getByRole('button', {
      name: 'Products',
      exact: true,
    });
    await expect(categoryNav).toBeVisible();
    await categoryNav.click();
    await expect(
      page.getByRole('button', { name: 'New entry', exact: true }).first()
    ).toBeVisible();

    const product = await createRootThroughUi(
      page,
      config.workspaceSlug,
      'Products',
      config.productName,
      productContent(config)
    );
    const productVersion = await publishEntry(
      page,
      config.workspaceSlug,
      product
    );

    const offer = await createInternalThroughUi(
      page,
      config.workspaceSlug,
      product.id,
      config.offerName,
      {
        product_version_id: String(productVersion.id),
        item: {
          sku: 'GOLDEN-PATH-001',
          variant_label: 'Default',
          quantity: 1,
          bundle_components: [],
        },
        landing_page_url: 'https://example.test/golden-path',
        market_scope: config.markets,
        claim_constraints: [],
        valid_from: '2026-01-01',
        valid_to: '2099-12-31',
        source_market_snapshot: {
          locale: 'en-US',
          market: config.markets[0] ?? 'US',
          placement: 'feed',
          country: config.markets[0] ?? 'US',
          currency: 'USD',
        },
      }
    );
    const offerVersion = await publishEntry(page, config.workspaceSlug, offer);

    const persona = await createRootThroughUi(
      page,
      config.workspaceSlug,
      'People',
      config.personaName,
      {
        name: config.personaName,
        person_type: 'fictional',
        age_expression: '25-35',
        prompt_fragments: ['approachable professional'],
        rights: {
          state: 'granted',
          regions: config.markets,
          channels: ['meta'],
        },
      }
    );
    const personaVersion = await publishEntry(
      page,
      config.workspaceSlug,
      persona
    );

    const template = await createRootThroughUi(
      page,
      config.workspaceSlug,
      'Templates',
      config.templateName,
      templateContent(config)
    );
    const templateVersion = await publishEntry(
      page,
      config.workspaceSlug,
      template
    );

    const promptPolicy = await createRootThroughUi(
      page,
      config.workspaceSlug,
      'Prompt Policies',
      config.promptPolicyName,
      promptPolicyContent()
    );
    const promptPolicyVersion = await publishEntry(
      page,
      config.workspaceSlug,
      promptPolicy
    );

    const dataSource = await createRootThroughUi(
      page,
      config.workspaceSlug,
      'Data Sources',
      config.dataSourceName,
      dataSourceContent(config.markets)
    );
    const dataSourceVersion = await publishEntry(
      page,
      config.workspaceSlug,
      dataSource
    );

    return {
      schemaVersion: 1,
      area: 'library',
      claims: [
        { code: 'product_created' as LibraryClaimCode, status: 'passed' },
        { code: 'offer_created' as LibraryClaimCode, status: 'passed' },
        { code: 'persona_created' as LibraryClaimCode, status: 'passed' },
        { code: 'template_applied' as LibraryClaimCode, status: 'passed' },
        { code: 'prompt_policy_created' as LibraryClaimCode, status: 'passed' },
        { code: 'data_source_created' as LibraryClaimCode, status: 'passed' },
      ],
      product: entityRef('product', product.id, 'same_origin_response'),
      productVersion: entityRef(
        'product_version',
        String(productVersion.id),
        'same_origin_response'
      ),
      offerVersion: entityRef(
        'offer_version',
        String(offerVersion.id),
        'same_origin_response'
      ),
      personaVersion: entityRef(
        'persona_version',
        String(personaVersion.id),
        'same_origin_response'
      ),
      templateVersion: entityRef(
        'template_version',
        String(templateVersion.id),
        'same_origin_response'
      ),
      promptPolicyVersion: entityRef(
        'prompt_policy_version',
        String(promptPolicyVersion.id),
        'same_origin_response'
      ),
      dataSourceVersion: entityRef(
        'data_source_version',
        String(dataSourceVersion.id),
        'same_origin_response'
      ),
    };
  }

  async verifyPersistedState(
    session: OwnerSessionCapability,
    evidence: LibraryEvidence
  ): Promise<void> {
    const page = session.page();
    const currentPath = new URL(page.url()).pathname;
    const workspaceSlug = currentPath.match(/^\/w\/([^/]+)/)?.[1];
    if (!workspaceSlug)
      throw new Error('Fresh owner context is not in a workspace');
    // A broker restart completes before the API is ready to serve reads.
    // Probe through the owner's actual BFF boundary before mounting a page
    // whose query could otherwise cache the transient startup failure.
    await expect
      .poll(
        async () => {
          const response = await page.request.get(
            `/api/domain/v1/w/${workspaceSlug}/library/entries/${encodeURIComponent(evidence.product.runtimeId)}`
          );
          if (!response.ok()) return null;
          return response.json();
        },
        { timeout: 30_000, intervals: [250, 500, 1000, 2000] }
      )
      .toMatchObject({
        id: evidence.product.runtimeId,
        current_version_id: evidence.productVersion.runtimeId,
      });
    await page.goto(
      `/w/${workspaceSlug}/library?cat=product&entry=${encodeURIComponent(evidence.product.runtimeId)}`
    );
    await page.waitForSelector('[data-testid="library-shell"]');
    await expect(page.locator('h2')).toBeVisible({ timeout: 15_000 });
  }
}
