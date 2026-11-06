import {
  test,
  expect,
  LOCALIZATION_BFF,
  LOCALIZATION_URL,
  createBatchThroughUi,
  pollItemUntil,
  prepareBatchThroughUi,
  readJson,
  runOneLocalizationWorkerJob,
  seedLocalizationPrerequisites,
  startLocalizationWorker,
  stopLocalizationWorker,
} from './fixtures';
import type {
  LocalizationBatchPayload,
  LocalizationItemPayload,
} from './fixtures';

/**
 * Isolated-output recovery, end to end, against the real stack (spec
 * §2E/§2H). No route is intercepted and no Batch, QC, publication or
 * binding state is manufactured in browser memory -- every assertion below
 * reads what FastAPI and PostgreSQL actually persisted.
 *
 * The scenario is the one the contract describes: a Batch is cancelled
 * while an indivisible edit call is already in flight. The call completes
 * anyway, its output is preserved but unpublished, and the Item is
 * `cancelled`. From there the only route to a published asset is the
 * explicit pair -- authorize the QC the Attempt still owes, wait for it,
 * then adopt.
 *
 * The long-lived Worker is stopped for the cancellation window. Disposable
 * invocations of that same real Worker then process one durable Job at a time
 * until the edit output exists. This gives the test a deterministic boundary
 * before the separately admitted QC Job can run, rather than racing a polling
 * request against a Worker that drains both jobs back-to-back.
 */

// The Worker is a single container shared by every spec in this suite, and
// this test stops it partway through. Without this hook a failure anywhere
// between the stop and the restart leaves it stopped, so the retry -- and any
// spec scheduled after it -- runs against a pipeline that never advances and
// fails somewhere else entirely. That is what turned one real assertion
// failure into three different ones across retries. `docker compose start` on
// an already-running container is a no-op, so this is safe on the happy path.
test.afterEach(() => {
  startLocalizationWorker();
});

async function csrfToken(page: import('@playwright/test').Page) {
  return (
    await readJson<{ token: string }>(await page.request.get('/api/csrf'))
  ).token;
}

async function domainPost(
  page: import('@playwright/test').Page,
  path: string,
  { data, ifMatch }: { data?: unknown; ifMatch?: string | number } = {}
) {
  const headers: Record<string, string> = {
    Origin: new URL(page.url()).origin,
    'X-CSRF-Token': await csrfToken(page),
    'X-Intent-Id': `localization-isolated-${Math.random().toString(36).slice(2)}`,
  };
  if (ifMatch !== undefined) headers['If-Match'] = String(ifMatch);
  return page.request.post(`${LOCALIZATION_BFF}${path}`, {
    headers,
    ...(data === undefined ? {} : { data }),
  });
}

test('recovers a preserved isolated output into a real published child, lineage and binding', async ({
  page,
}) => {
  test.setTimeout(300_000);
  const seed = seedLocalizationPrerequisites();
  const readyMarket = seed.markets.find(
    (market) => market.expectedReadiness === 'ready'
  );
  expect(readyMarket).toBeDefined();

  const batchId = await createBatchThroughUi(page, seed, [readyMarket!]);
  await prepareBatchThroughUi(page, seed, batchId, 1);

  // Confirm through the domain API rather than the UI button so the frozen
  // recipe actually names an external QC step -- the UI's confirm form
  // freezes an empty plan, which is the one shape that can never leave an
  // Attempt owing QC.
  const batchBeforeConfirm = await readJson<LocalizationBatchPayload>(
    await page.request.get(`${LOCALIZATION_BFF}/batches/${batchId}`)
  );
  // Stop before confirmation publishes the edit + QC Jobs. Once confirmation
  // returns, only the one-job Worker ticks below are allowed to advance this
  // queue, so observing an output cannot lose a race to the next QC claim.
  stopLocalizationWorker();
  const confirmResponse = await domainPost(
    page,
    `/batches/${batchId}/confirm`,
    {
      ifMatch: batchBeforeConfirm.revision,
      data: {
        edit_provider: 'mock',
        edit_model: 'localization-edit-v1',
        edit_adapter_version: 'v1',
        external_qc_steps: ['ocr_check'],
        max_attempts: 3,
      },
    }
  );
  expect(confirmResponse.status()).toBe(200);
  const confirmed = await readJson<{ item_ids: string[] }>(confirmResponse);
  const itemId = confirmed.item_ids[0];
  expect(itemId).toMatch(/^li_/);

  // --- the isolated output -------------------------------------------------
  // Each disposable Worker exits after one Job. Confirmation pre-admits the
  // edit and frozen external-QC Jobs with the same timestamp, so UUID ordering
  // may let the not-yet-runnable QC Job be claimed first. Keep ticking the real
  // queue until this Item's edit output is durable; the successful edit tick
  // then exits before any QC Job can be claimed.
  let beforeCancel: LocalizationItemPayload | undefined;
  for (let tick = 0; tick < 20; tick += 1) {
    runOneLocalizationWorkerJob();
    beforeCancel = await readJson<LocalizationItemPayload>(
      await page.request.get(
        `${LOCALIZATION_BFF}/batches/${batchId}/items/${itemId}`
      )
    );
    if (beforeCancel.attempts.some((attempt) => Boolean(attempt.output))) {
      break;
    }
  }
  expect(
    beforeCancel,
    'the one-job Worker ticks must expose the persisted edit output'
  ).toBeDefined();
  const isolated = beforeCancel?.attempts.find((attempt) => attempt.output);
  expect(isolated, 'the edit call must have persisted an output').toBeTruthy();
  expect(
    isolated!.publication,
    'a one-job Worker tick must not advance through publication'
  ).toBeFalsy();
  // Absence of a publication is not enough: premature adoption below needs
  // the frozen QC plan to still be incomplete. Assert the one-job boundary's
  // real invariant here, where a harness regression is immediately obvious.
  expect(
    isolated!.qc.some(
      (check) => check.check_kind === 'ocr_check' && check.result === 'pass'
    ),
    'a one-job Worker tick advanced beyond the edit into external QC'
  ).toBeFalsy();
  const attemptId = isolated!.id;

  const batchBeforeCancel = await readJson<LocalizationBatchPayload>(
    await page.request.get(`${LOCALIZATION_BFF}/batches/${batchId}`)
  );
  const cancelResponse = await domainPost(page, `/batches/${batchId}/cancel`, {
    ifMatch: batchBeforeCancel.revision,
  });
  expect(cancelResponse.status()).toBe(200);

  const cancelled = await readJson<LocalizationItemPayload>(
    await page.request.get(
      `${LOCALIZATION_BFF}/batches/${batchId}/items/${itemId}`
    )
  );
  expect(cancelled.state).toBe('cancelled');
  expect(
    cancelled.attempts.find((attempt) => attempt.id === attemptId)?.output
  ).toBeTruthy();

  // --- adopting too early is refused, not silently accepted ---------------
  const prematureAdopt = await domainPost(
    page,
    `/batches/${batchId}/items/${itemId}/attempts/${attemptId}/adopt`
  );
  expect(prematureAdopt.status()).toBe(422);
  const problem = (await prematureAdopt.json()) as {
    code: string;
    findings?: string[];
  };
  expect(problem.code).toBe('batch_validation_failed');
  expect(problem.findings).toContain('qc_plan_incomplete');
  expect(
    problem.findings?.some((finding) =>
      finding.startsWith('external_qc_missing:')
    )
  ).toBeTruthy();

  // Nothing was published by the refusal.
  const afterRefusal = await readJson<LocalizationItemPayload>(
    await page.request.get(
      `${LOCALIZATION_BFF}/batches/${batchId}/items/${itemId}`
    )
  );
  expect(afterRefusal.state).toBe('cancelled');
  expect(afterRefusal.binding).toBeFalsy();

  // --- authorize the QC the Attempt still owes ----------------------------
  const authorizeResponse = await domainPost(
    page,
    `/batches/${batchId}/items/${itemId}/attempts/${attemptId}/authorize-isolated-qc`
  );
  expect(authorizeResponse.status()).toBe(200);
  const authorized = await readJson<{
    authorized_steps: string[];
    operation_id: string | null;
  }>(authorizeResponse);
  expect(authorized.authorized_steps).toEqual(['ocr_check']);
  expect(authorized.operation_id).toMatch(/^op_/);

  startLocalizationWorker();
  await pollItemUntil(page, batchId, itemId, (item) =>
    (item.attempts.find((attempt) => attempt.id === attemptId)?.qc ?? []).some(
      (check) => check.check_kind === 'ocr_check' && check.result === 'pass'
    )
  );

  // Authorized QC records its evidence and stops. It must never publish on
  // the user's behalf -- adoption is the explicit command.
  const afterQc = await readJson<LocalizationItemPayload>(
    await page.request.get(
      `${LOCALIZATION_BFF}/batches/${batchId}/items/${itemId}`
    )
  );
  expect(afterQc.state).toBe('cancelled');
  expect(
    afterQc.attempts.find((attempt) => attempt.id === attemptId)?.publication
  ).toBeFalsy();

  // --- adopt --------------------------------------------------------------
  const adoptResponse = await domainPost(
    page,
    `/batches/${batchId}/items/${itemId}/attempts/${attemptId}/adopt`
  );
  expect(adoptResponse.status()).toBe(200);
  const adopted = await readJson<{
    asset_version_id: string;
    publication_id: string;
    binding_id: string;
    replayed: boolean;
  }>(adoptResponse);
  expect(adopted.replayed).toBe(false);
  expect(adopted.asset_version_id).toMatch(/^av_/);
  expect(adopted.publication_id).toMatch(/^lop_/);
  expect(adopted.binding_id).toBeTruthy();

  // --- the persisted result -----------------------------------------------
  const published = await readJson<LocalizationItemPayload>(
    await page.request.get(
      `${LOCALIZATION_BFF}/batches/${batchId}/items/${itemId}`
    )
  );
  expect(published.state).toBe('succeeded');
  expect(published.reason_code).toBe('adopted_isolated_output');

  const publishedAttempt = published.attempts.find(
    (attempt) => attempt.id === attemptId
  );
  expect(publishedAttempt?.publication).toMatchObject({
    id: adopted.publication_id,
    publication_kind: 'adopted',
    asset_version_id: adopted.asset_version_id,
  });

  expect(published.lineage).toMatchObject({
    localized_asset_version_id: adopted.asset_version_id,
  });
  expect(published.binding).toMatchObject({
    bound_asset_version_id: adopted.asset_version_id,
    // The item projection exposes this as `kind`; `binding_kind` is only the
    // column name. golden-path.spec.ts has it right; this file did not.
    kind: 'localized',
    // spec §16.11: the localized child starts unreviewed and is not
    // experiment-ready; adoption inherits nothing from what it superseded.
    review_state: 'pending_review',
    ready_for_experiment: false,
  });

  // --- and the same evidence in the browser -------------------------------
  await page.goto(`${LOCALIZATION_URL}?batch=${batchId}&item=${itemId}`);
  const inspector = page
    .getByRole('heading', { name: 'Item attempt & QC' })
    .locator('xpath=ancestor::section');
  // The component renders `Output: published` in one element, so no element's
  // text is exactly `published` and `getByText(..., { exact: true })` could
  // never match however long it waited -- it spent the full 60s timeout every
  // run that got this far. Assert the whole label, the way golden-path.spec.ts
  // does: it is also the stronger check, since a bare `published` could match
  // the word anywhere in the section.
  await expect(inspector).toContainText('Output: published', {
    timeout: 60_000,
  });
  await expect(
    inspector.getByTestId(`adopt-isolated-output-${attemptId}`)
  ).toHaveCount(0);
  await expect(inspector.getByText(adopted.asset_version_id)).toBeVisible();

  // A second adoption is a replay, not a second publication.
  const replayResponse = await domainPost(
    page,
    `/batches/${batchId}/items/${itemId}/attempts/${attemptId}/adopt`
  );
  expect(replayResponse.status()).toBe(200);
  const replayed = await readJson<{
    replayed: boolean;
    asset_version_id: string;
  }>(replayResponse);
  expect(replayed.replayed).toBe(true);
  expect(replayed.asset_version_id).toBe(adopted.asset_version_id);
});

test('the Truth panel never reports an accepted source attempt as a failure', async ({
  page,
}) => {
  test.setTimeout(180_000);
  const seed = seedLocalizationPrerequisites();
  const readyMarket = seed.markets.find(
    (market) => market.expectedReadiness === 'ready'
  );
  expect(readyMarket).toBeDefined();

  const batchId = await createBatchThroughUi(page, seed, [readyMarket!]);
  await prepareBatchThroughUi(page, seed, batchId, 1);

  // The persisted evidence: a ready market resolves its facts through
  // `accepted` source attempts.
  const batch = await readJson<LocalizationBatchPayload>(
    await page.request.get(`${LOCALIZATION_BFF}/batches/${batchId}`)
  );
  const market = batch.markets.find(
    (entry) => entry.market === readyMarket!.market
  );
  expect(market?.readiness).toBe('ready');
  const attempts = market?.source_attempts ?? [];
  expect(attempts.length).toBeGreaterThan(0);
  expect(attempts.every((attempt) => attempt.outcome === 'accepted')).toBe(
    true
  );

  await page.goto(`${LOCALIZATION_URL}?batch=${batchId}`);
  const truthPanel = page
    .getByRole('heading', { name: 'Truth package' })
    .locator('xpath=ancestor::section');
  await expect(truthPanel).toBeVisible();

  // The panel used to test `outcome !== 'resolved'`, a value the backend
  // never emits, so every accepted attempt was listed as a source failure
  // and a fully ready market looked broken.
  await expect(truthPanel.getByText('Source failures / skips')).toHaveCount(0);
});
