import type { Page } from '@playwright/test';
import { randomUUID } from 'node:crypto';

import type { OwnerSessionCapability } from './onboarding-harness';

export type MetaMode = 'customer_sandbox';

export interface MetaSandboxConnectionResult {
  connected: boolean;
  mode: MetaMode;
}

// MetaConnectionSection renders these instead of its next state when the
// domain refuses a request; each carries the problem code and trace id.
const META_CONNECTION_PROBLEMS = [
  'meta-connection-list-problem',
  'meta-connection-create-problem',
];

/**
 * Waits for `testId` in the Meta connection section, or fails with the
 * problem the section reports instead. The golden path runs without traces,
 * so the refusal's code and trace id are the evidence a failed run keeps.
 */
async function expectMetaConnectionState(
  page: Page,
  testId: string
): Promise<void> {
  const settled = page
    .locator(
      [testId, ...META_CONNECTION_PROBLEMS]
        .map((id) => `[data-testid="${id}"]`)
        .join(', ')
    )
    .first();
  await settled.waitFor();
  if ((await settled.getAttribute('data-testid')) !== testId) {
    throw new Error(
      `Meta connection section reported instead of ${testId}: ${(
        await settled.innerText()
      ).trim()}`
    );
  }
}

export class WorkspaceSetup {
  private readonly baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl;
  }

  async createMetaSandboxConnection(
    session: OwnerSessionCapability,
    opts: { mode: MetaMode }
  ): Promise<MetaSandboxConnectionResult> {
    const page = session.page();

    await page.click('[data-testid="workspace-settings-link"]');
    await page.waitForSelector('[data-testid="meta-connection-section"]');

    await expectMetaConnectionState(page, 'create-meta-connection');
    await page.click('[data-testid="create-meta-connection"]');
    await page.waitForSelector('[data-testid="meta-connection-mode-select"]');

    await page.selectOption(
      '[data-testid="meta-connection-mode-select"]',
      opts.mode
    );

    await page.click('[data-testid="confirm-meta-connection"]');

    await expectMetaConnectionState(page, 'meta-connection-status-active');

    // A Meta connection and its Data Source Binding are separate governed
    // records. The Settings surface creates only the connection, while the
    // multi-candidate Launch preview requires an active binding for the
    // connection's sandbox account. Provision that binding through the same
    // same-origin BFF boundary so launch setup is complete and tenant-scoped.
    const workspaceMatch = new URL(page.url()).pathname.match(/\/w\/([^/]+)/);
    if (!workspaceMatch) throw new Error('Workspace URL is missing its slug');
    const workspaceSlug = decodeURIComponent(workspaceMatch[1]);
    const connectionsResponse = await page.request.get(
      `/api/domain/v1/w/${workspaceSlug}/meta/connections?meta_mode=${opts.mode}`
    );
    if (!connectionsResponse.ok()) {
      throw new Error(
        `Meta connection query failed (${connectionsResponse.status()}): ${await connectionsResponse.text()}`
      );
    }
    const connectionsBody = (await connectionsResponse.json()) as {
      items?: Array<{ id: string; ad_account_id: string; state: string }>;
    };
    const connection = connectionsBody.items?.find(
      (item) => item.state === 'active'
    );
    if (!connection) throw new Error('Active Meta connection was not returned');

    const bindingsResponse = await page.request.get(
      `/api/domain/v1/w/${workspaceSlug}/meta/connections/${connection.id}/bindings?meta_mode=${opts.mode}`
    );
    if (!bindingsResponse.ok()) {
      throw new Error(
        `Meta binding query failed (${bindingsResponse.status()}): ${await bindingsResponse.text()}`
      );
    }
    const bindingsBody = (await bindingsResponse.json()) as {
      items?: Array<{ ad_account_id: string; state: string }>;
    };
    const hasActiveBinding = bindingsBody.items?.some(
      (item) =>
        item.state === 'active' &&
        item.ad_account_id === connection.ad_account_id
    );
    if (!hasActiveBinding) {
      const csrfResponse = await page.request.get('/api/csrf');
      const csrfBody = (await csrfResponse.json()) as { token?: string };
      if (!csrfResponse.ok() || !csrfBody.token) {
        throw new Error(
          'CSRF token request failed while creating Meta binding'
        );
      }
      const origin = new URL(page.url()).origin;
      const bindingResponse = await page.request.post(
        `/api/domain/v1/w/${workspaceSlug}/meta/connections/${connection.id}/bindings`,
        {
          data: {
            meta_mode: opts.mode,
            ad_account_id: connection.ad_account_id,
          },
          headers: {
            'X-CSRF-Token': csrfBody.token,
            'X-Intent-Id': randomUUID(),
            'X-Trace-Id': `golden-path-${randomUUID()}`,
            Origin: origin,
            Referer: page.url(),
          },
        }
      );
      if (!bindingResponse.ok()) {
        throw new Error(
          `Meta binding creation failed (${bindingResponse.status()}): ${await bindingResponse.text()}`
        );
      }
    }

    return {
      connected: true,
      mode: opts.mode,
    };
  }
}
