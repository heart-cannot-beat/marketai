import {
  test,
  expect,
  scaleUrl,
  scaleEnrollmentUrl,
  scaleBff,
  readJson,
  seedCustomerSandboxChain,
  inspectEnrollment,
  inspectEpisode,
  inspectZeroMetaWrites,
  restartScaleApiServer,
  restartScaleWorker,
  installNetworkSpy,
} from './fixtures';
import type { ScaleChainSeed, NetworkSpy } from './fixtures';

/**
 * Customer-Sandbox smoke (spec
 * docs/product/scale-advice-semantics-contract.md §4.5, issue #87 Task 9).
 * Replaces the documented-but-unexecuted
 * `tests/e2e/test_scale_customer_sandbox_smoke.py` checklist this task
 * deletes.
 *
 * Per spec §4.5, this suite uses the fixed, persistent Meta Sandbox
 * adapter: `seedCustomerSandboxChain()` never injects a fault config and
 * `seed_prerequisites.py seed-customer-sandbox` never rewrites the fixed
 * Sandbox scenario -- it only proves the Winner/DeliveryTarget lineage
 * against `customer_sandbox`, exactly the real command path a browser
 * enrolling against Sandbox data would use.
 */
test.describe.serial('Scale Customer-Sandbox smoke (spec §4.5)', () => {
  let seed: ScaleChainSeed;
  let networkSpy: NetworkSpy;
  let enrollmentId: string;
  let episodeId: string;

  test('1: explicit enrollment and activation against persisted Sandbox data', async ({
    page,
  }) => {
    seed = seedCustomerSandboxChain();
    expect(seed.metaMode).toBe('customer_sandbox');

    networkSpy = installNetworkSpy(page);
    await page.goto(scaleUrl());
    await expect(page.getByRole('heading', { name: 'Scale' })).toBeVisible();

    // The list defaults to `customer_sandbox` only if
    // `useScaleMetaMode`'s stored preference already says so; select it
    // explicitly so this smoke never silently reads a different mode's
    // Enrollment list (spec §4.5's "no fallback" guarantee, proven again
    // in test 4 below).
    const modeSelect = page.getByLabel('Meta mode');
    if (await modeSelect.isVisible().catch(() => false)) {
      await modeSelect.selectOption('customer_sandbox');
    }

    await page.getByRole('button', { name: 'New Enrollment' }).click();
    const dialog = page.locator(
      'dialog[aria-labelledby="candidate-selector-title"]'
    );
    await expect(dialog).toBeVisible();
    const decisionButton = dialog.getByText(seed.decisionId, { exact: false });
    await expect(decisionButton).toBeVisible({ timeout: 30_000 });
    await decisionButton.click();
    await dialog.getByLabel('Delivery target ID').fill(seed.deliveryTargetId);

    const [enrollmentResponse] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          new URL(r.url()).pathname ===
            `${scaleBff('customer_sandbox')}/enrollments`
      ),
      dialog.getByRole('button', { name: 'Create' }).click(),
    ]);
    const created = await readJson<{
      id: string;
      lifecycle: string;
      meta_mode: string;
    }>(enrollmentResponse);
    expect(created.meta_mode).toBe('customer_sandbox');
    enrollmentId = created.id;

    await page.goto(scaleEnrollmentUrl(enrollmentId));
    await page.locator('[data-action="activate"]').click();
    const activateDialog = page.locator(
      'dialog[aria-labelledby="activate-dialog-title"]'
    );
    await activateDialog
      .getByRole('button', { name: 'Create preview' })
      .click();
    await expect(activateDialog.getByText('Activation preview ID')).toBeVisible(
      { timeout: 20_000 }
    );

    const [activateResponse] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'PUT' &&
          new URL(r.url()).pathname ===
            `${scaleBff('customer_sandbox')}/enrollments/${enrollmentId}/activate`
      ),
      activateDialog.getByRole('button', { name: 'Activate' }).click(),
    ]);
    const activated = await readJson<{
      lifecycle: string;
      current_episode_id: string;
      meta_mode: string;
    }>(activateResponse);
    expect(activated.lifecycle).toBe('active');
    expect(activated.meta_mode).toBe('customer_sandbox');
    episodeId = activated.current_episode_id;
  });

  test('2: refresh and process restart recovery', async ({ page }) => {
    const before = inspectEnrollment(
      seed.workspaceId,
      'customer_sandbox',
      enrollmentId
    );

    await page.reload();
    await restartScaleApiServer();
    restartScaleWorker();

    await page.goto(scaleEnrollmentUrl(enrollmentId));
    await expect(
      page.getByRole('heading', { name: enrollmentId })
    ).toBeVisible();

    const after = inspectEnrollment(
      seed.workspaceId,
      'customer_sandbox',
      enrollmentId
    );
    expect(after.lifecycle).toBe(before.lifecycle);
    expect(after.revision).toBe(before.revision);
    expect(after.current_episode_id).toBe(before.current_episode_id);

    const episode = inspectEpisode(
      seed.workspaceId,
      'customer_sandbox',
      episodeId
    );
    expect(episode.found).toBe(true);
  });

  test('3: customer_sandbox provenance is visible on every projection', async ({
    page,
  }) => {
    await page.goto(scaleEnrollmentUrl(enrollmentId));
    await expect(page.getByText('Customer sandbox')).toBeVisible();

    const enrollmentResponse = await page.request.get(
      `${scaleBff('customer_sandbox')}/enrollments/${enrollmentId}`
    );
    const enrollment = await readJson<{ meta_mode: string }>(
      enrollmentResponse
    );
    expect(enrollment.meta_mode).toBe('customer_sandbox');

    const episodeResponse = await page.request.get(
      `${scaleBff('customer_sandbox')}/enrollments/${enrollmentId}/episodes/${episodeId}`
    );
    const episode = await readJson<{ meta_mode?: string }>(episodeResponse);
    // Episode responses are scoped by the URL's own meta_mode path
    // segment, not a body field the client could spoof -- a 200 at all
    // from the `customer_sandbox` path segment is itself the provenance
    // proof; assert the body never claims a different mode if it happens
    // to echo one back.
    if (episode.meta_mode) expect(episode.meta_mode).toBe('customer_sandbox');
  });

  test('4: no fallback to controlled_mock or internal_validation', async ({
    page,
  }) => {
    // Reading this exact Enrollment id through a different meta_mode path
    // segment must fail closed, never silently serve (or fabricate)
    // another mode's projection of the same id.
    const asMock = await page.request.get(
      `${scaleBff('controlled_mock')}/enrollments/${enrollmentId}`
    );
    expect([403, 404]).toContain(asMock.status());

    const asInternal = await page.request.get(
      `${scaleBff('internal_validation')}/enrollments/${enrollmentId}`
    );
    expect([400, 403, 404, 422]).toContain(asInternal.status());
  });

  test('5: any Advice/Outcome stays non-production and same-mode', async ({
    page,
  }) => {
    const threadsResponse = await page.request.get(
      `${scaleBff('customer_sandbox')}/advice-threads`
    );
    const threads = await readJson<{ items: Array<{ id: string }> }>(
      threadsResponse
    );
    // The fixed Sandbox adapter is not fault-injected, so this smoke does
    // not force fatigue/health Advice to appear -- if any is already
    // present from prior Sandbox activity, every thread must still be
    // reachable only under `customer_sandbox`.
    for (const thread of threads.items ?? []) {
      const crossMode = await page.request.get(
        `${scaleBff('controlled_mock')}/advice-threads`
      );
      const crossModeBody = await readJson<{ items: Array<{ id: string }> }>(
        crossMode
      );
      expect(crossModeBody.items.some((t) => t.id === thread.id)).toBe(false);
    }

    expect(networkSpy.offendingRequests).toEqual([]);
    const dbCheck = inspectZeroMetaWrites(seed.workspaceId);
    expect(dbCheck.offending).toEqual([]);
    expect(dbCheck.meta_write_token_empty).toBe(true);
    expect(dbCheck.meta_write_ready).toBe(false);
  });
});
