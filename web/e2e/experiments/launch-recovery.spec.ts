import { expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import {
  test,
  WORKSPACE_SLUG,
  EXPERIMENTS_URL,
  EXPERIMENTS_BFF,
  seedPrerequisites,
  readJson,
  domainMutationHeaders,
  acknowledgeFreeze,
  expectFrozenPanel,
} from './fixtures';
import type { ExperimentsPrerequisites } from './fixtures';
import {
  inspectFakeMetaObjects,
  inspectCallIntents,
  inspectJobsForOperation,
  claimWorkerCheckpoint,
  restartApiServer,
  restartWorker,
  stopWorker,
  startWorker,
  waitForOperationState,
} from './helpers';
import type { components } from '@/lib/domain-client/generated/domain-v1';

type ExperimentDetail = components['schemas']['ExperimentDetail'];
type ExperimentEntryMutation = components['schemas']['ExperimentEntryMutation'];
type MultiCandidatePreviewResponse =
  components['schemas']['MultiCandidatePreviewResponse'];
type MultiCandidateConfirmationResponse =
  components['schemas']['MultiCandidateConfirmationResponse'];
type LaunchProvisioningAccepted =
  components['schemas']['LaunchProvisioningAccepted'];
type LaunchProvisioningResponse =
  components['schemas']['LaunchProvisioningResponse'];
type LaunchRecoveryResponse = components['schemas']['LaunchRecoveryResponse'];

/**
 * Real-backend E2E coverage for issue #88's launch-recovery scenarios (SDD
 * Scenario 4, 5a-c, 9). Consumes the Task 10 infrastructure -- `fixtures.ts`
 * (`seedPrerequisites`), `helpers.ts` (`inspectFakeMetaObjects`,
 * `inspectCallIntents`, `inspectJobsForOperation`, `claimWorkerCheckpoint`,
 * `restartApiServer`/`restartWorker`/`stopWorker`/`startWorker`,
 * `waitForOperationState`) and `worker_checkpoint.py` -- and drives every
 * freeze / launch-preview / confirm / execute / reconcile step through the
 * real, rendered `ExperimentsWorkspace` UI
 * (`web/components/experiments/ExperimentsWorkspace.tsx`) against a live
 * PostgreSQL/FastAPI/Worker stack, exactly like `duplicate-resolution.spec.ts`
 * (Task 11). Only pre-UI setup (experiment + entry creation) goes through
 * direct `page.request` calls -- this suite never intercepts
 * `/api/domain/**` or manufactures preview/provisioning/reconciliation state
 * in browser memory.
 *
 * Each scenario seeds its own fresh, tag-scoped `controlled_mock`
 * MetaConnection + ScenarioVersion via `seedPrerequisites(faultConfig)`
 * (see `seed_prerequisites.py` and `app/adapters/meta/fake_adapter.py`'s
 * `_match_fault`/`ensure_object`) so fault rules never leak between
 * scenarios sharing one Playwright worker/database.
 *
 * Every scenario provisions through `app/launch/saga.py`'s
 * `execute_provisioning` (run by the Worker's `launch_provisioning` Job --
 * see `app/worker_runtime.py::_launch_provisioning`) and, where an Operation
 * lands `uncertain`, resolves it through `app/launch/recovery.py`'s
 * `execute_recovery` (the Worker's `launch_recovery` Job). Scenario 5b/5c
 * also exercise `app/platform/jobs.py::recover_expired_jobs`'s lease-expiry
 * path (leased-but-not-running Jobs are safely requeued; running Jobs whose
 * lease expires without a recorded outcome are left `uncertain`, and for
 * `launch_provisioning` Jobs specifically, the owning Operation is moved to
 * `uncertain` too -- see that function's `launch_provisioning` branch).
 * Scenario 5c's "wait for lease expiry" step assumes the E2E stack's
 * `JOB_LEASE_SECONDS` is configured short (see `app/config.py`); Scenario
 * 5b sidesteps that entirely by using `worker_checkpoint.py`'s own
 * `--lease-seconds` override instead of the Worker's default lease.
 *
 * These tests cannot run without a live Postgres/API/Worker stack backed by
 * docker-compose (`E2E_EXPERIMENTS_COMPOSE_PROJECT`, see `helpers.ts`).
 * They are written to be structurally correct and type-safe against the
 * real OpenAPI contract (`lib/domain-client/generated/domain-v1.d.ts`) and
 * the real component/service source, not to be executed in this sandbox.
 */

const LAUNCH_BFF = `/api/domain/v1/w/${WORKSPACE_SLUG}/launch`;
const EXECUTE_ACK_TEXT_PREFIX = 'Execute this exact';
const LAUNCH_ACK_TEXT_PREFIX =
  'I reviewed every exact candidate AssetVersion and creative configuration';

/** Mirrors `toDraftContext` in `ExperimentsWorkspace.tsx` and satisfies
 * `app.experiments.service._validate_draft_context`'s schema-2 rules. */
function draftContext(seed: ExperimentsPrerequisites) {
  const start = new Date(Date.now() + 24 * 60 * 60 * 1000);
  const end = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000);
  return {
    market: 'US',
    currency: 'USD',
    placement: 'feed',
    product_version_id: seed.productVersionId,
    offer_version_id: seed.offerVersionId,
    // Within the Sandbox Launch cap (`LAUNCH_MAX_DAILY_BUDGET_MINOR`, 500
    // minor units): the Launch form pre-fills its per-candidate budget
    // from this daily amount.
    budget: { daily_amount: '5.00', total_amount: '150.00' },
    schedule: {
      start_at: `${start.toISOString().slice(0, 10)}T00:00:00+00:00`,
      end_at: `${end.toISOString().slice(0, 10)}T23:59:59+00:00`,
      timezone: 'UTC',
    },
    audience: { schema_version: '1', content: { targeting: 'broad' } },
    attribution: { click_window_days: 7, view_window_days: 1 },
    statistical_plan: {
      primary_metric: 'ctr',
      ctr_threshold: '0.01',
      alpha: '0.05',
      bootstrap_samples: 2000,
    },
  };
}

/**
 * Creates a draft Experiment with exactly two entries -- one member of the
 * seed's duplicate pair plus the independent candidate -- which never share
 * a `DimensionConceptKey`, so `analyze_duplicates` reports zero duplicate
 * groups (same non-duplicate combination as `duplicate-resolution.spec.ts`'s
 * Scenario 8). Launch recovery is orthogonal to duplicate resolution, so
 * every scenario here uses the simplest freeze path: an empty
 * `concept_resolution_plan`.
 */
async function createTwoCandidateExperiment(
  page: Page,
  seed: ExperimentsPrerequisites,
  name: string
): Promise<ExperimentDetail> {
  const createResponse = await page.request.post(EXPERIMENTS_BFF, {
    headers: await domainMutationHeaders(page),
    data: {
      product_id: seed.productId,
      name,
      draft_context: draftContext(seed),
    },
  });
  const created = await readJson<ExperimentDetail>(createResponse);

  let revision = created.revision;
  for (const assetVersionId of [
    seed.duplicatePairAssetVersionIds[0],
    seed.independentAssetVersionId,
  ]) {
    const entryResponse = await page.request.post(
      `${EXPERIMENTS_BFF}/${created.id}/entries`,
      {
        headers: await domainMutationHeaders(page),
        data: { asset_version_id: assetVersionId, expected_revision: revision },
      }
    );
    const entry = await readJson<ExperimentEntryMutation>(entryResponse);
    revision = entry.revision;
  }

  return readJson<ExperimentDetail>(
    await page.request.get(`${EXPERIMENTS_BFF}/${created.id}`)
  );
}

/**
 * Opens the Experiment, freezes it with an empty (no-duplicate)
 * `concept_resolution_plan`, and returns the frozen `ExperimentDetail`.
 */
async function openAndFreeze(
  page: Page,
  experimentId: string,
  experimentName: string
): Promise<ExperimentDetail> {
  await page.goto(`${EXPERIMENTS_URL}?experiment=${experimentId}`);
  await expect(
    page.getByRole('heading', { name: experimentName })
  ).toBeVisible();

  await acknowledgeFreeze(page);
  const [freezeResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname ===
          `${EXPERIMENTS_BFF}/${experimentId}/freeze`
    ),
    page.getByRole('button', { name: 'Freeze experiment' }).click(),
  ]);
  const frozen = await readJson<ExperimentDetail>(freezeResponse);
  expect(frozen.state).toBe('frozen');
  await expectFrozenPanel(page);
  expect(frozen.concept_resolution_evidence?.had_duplicates).toBe(false);
  return frozen;
}

/** The `<section>` carrying the "PAUSED launch preview" `<h3>`. */
function launchSection(page: Page) {
  return page
    .getByRole('heading', { name: 'PAUSED launch preview' })
    .locator('xpath=ancestor::section');
}

/**
 * Drives Create -> Confirm -> Execute for one `controlled_mock` launch,
 * exactly the sequence `duplicate-resolution.spec.ts`'s golden path drives
 * for `customer_sandbox` (steps 7-9 of its Scenario 1), but selecting
 * `controlled_mock` via the "Evidence mode" select first (`#meta-mode-select`
 * -- `MetaMode` in `ExperimentsWorkspace.tsx`).
 */
async function executeControlledMockLaunch(
  page: Page,
  seed: ExperimentsPrerequisites
): Promise<{
  preview: MultiCandidatePreviewResponse;
  confirmed: MultiCandidateConfirmationResponse;
  accepted: LaunchProvisioningAccepted;
}> {
  await page.locator('#meta-mode-select').selectOption('controlled_mock');
  await page
    .getByLabel('Sandbox Meta connection ID')
    .fill(seed.controlledMockConnectionId);
  await page
    .getByLabel('Sandbox Meta ad account ID')
    .fill(seed.controlledMockAdAccountId);

  const [previewResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname === `${LAUNCH_BFF}/previews`
    ),
    page.getByRole('button', { name: 'Create Sandbox preview' }).click(),
  ]);
  const preview =
    await readJson<MultiCandidatePreviewResponse>(previewResponse);
  expect(preview.candidate_count).toBe(2);
  expect(preview.state).toBe('draft');
  expect(preview.meta_mode).toBe('controlled_mock');

  await page.getByText(LAUNCH_ACK_TEXT_PREFIX, { exact: false }).click();
  const [confirmResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname ===
          `${LAUNCH_BFF}/previews/${preview.id}/confirm`
    ),
    page.getByRole('button', { name: 'Confirm Sandbox preview' }).click(),
  ]);
  const confirmed =
    await readJson<MultiCandidateConfirmationResponse>(confirmResponse);
  expect(confirmed.state).toBe('confirmed');

  await launchSection(page)
    .getByText(EXECUTE_ACK_TEXT_PREFIX, { exact: false })
    .click();
  const [executeResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname ===
          `${LAUNCH_BFF}/previews/${preview.id}/execute`
    ),
    page.getByRole('button', { name: 'Execute PAUSED Sandbox launch' }).click(),
  ]);
  const accepted = await readJson<LaunchProvisioningAccepted>(executeResponse);
  await expect(page).toHaveURL(
    new RegExp(`[?&]launchProvisioning=${accepted.id}(?:&|$)`)
  );

  return { preview, confirmed, accepted };
}

/** Polls `inspectCallIntents` until one intent for `metaApiMethod` carries a
 * non-null dispatch marker -- the point past which "no outcome" means
 * "uncertain", not "never happened" (spec §5.3/§8.3). */
async function waitForDispatchMarker(
  workspaceId: string,
  operationId: string,
  metaApiMethod: string,
  timeoutMs = 30_000
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const intents = inspectCallIntents(workspaceId, operationId);
    if (
      intents.some(
        (intent) =>
          intent.meta_api_method === metaApiMethod &&
          intent.network_dispatched_at !== null
      )
    ) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(
    `No dispatched ${metaApiMethod} intent observed for operation ${operationId} within ${timeoutMs}ms`
  );
}

test.describe('Launch recovery', () => {
  test.describe.configure({ mode: 'serial' });

  test('Scenario 4: uncertain dispatch → reconciliation → no duplicates', async ({
    page,
  }) => {
    test.setTimeout(300_000);

    // The saga creates one "ad" per candidate, in candidate order (spec
    // §5.1-§5.7's per-candidate AdSet+Ad loop). With two candidates,
    // match_ordinal 2 targets the *final* ad -- exactly the one this
    // scenario needs uncertain.
    const seed = seedPrerequisites({
      rules: [
        {
          object_type: 'ad',
          match_ordinal: 2,
          effect: 'uncertain_after_apply',
        },
      ],
    });
    const experimentName = `E2E launch recovery uncertain dispatch ${seed.tag}`;

    const created = await createTwoCandidateExperiment(
      page,
      seed,
      experimentName
    );
    expect(created.entries).toHaveLength(2);
    await openAndFreeze(page, created.id, experimentName);

    const { accepted } = await executeControlledMockLaunch(page, seed);

    // `uncertain_after_apply` fires after the FakeMetaObjectRecord for the
    // final ad is already durably created -- `_execute_step` (saga.py)
    // catches the adapter exception and raises `ProvisioningUncertain`,
    // which `worker_runtime._launch_provisioning` turns into
    // `OperationState.UNCERTAIN` and `provisioning_state =
    // "partially_provisioned"`.
    const uncertain = (await waitForOperationState(
      page,
      WORKSPACE_SLUG,
      accepted.id,
      'controlled_mock',
      'uncertain',
      60_000
    )) as LaunchProvisioningResponse;
    expect(uncertain.provisioning_state).toBe('partially_provisioned');

    // Reconcile through the UI -- `recoverLaunch()` in
    // `ExperimentsWorkspace.tsx` POSTs `MetaModeRequest` to
    // `.../operations/{operation_id}/recovery` (`recover_meta_launch_operation`).
    const reconcileButton = page.getByRole('button', {
      name: 'Reconcile uncertain operation',
    });
    await expect(reconcileButton).toBeVisible();
    const [recoveryResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${LAUNCH_BFF}/operations/${uncertain.operation_id}/recovery`
      ),
      reconcileButton.click(),
    ]);
    expect(recoveryResponse.status()).toBe(202);
    const recovery = await readJson<LaunchRecoveryResponse>(recoveryResponse);
    expect(recovery.original_operation_id).toBe(uncertain.operation_id);
    expect(recovery.state).toBe('open');

    // The Worker's `launch_recovery` Job runs `execute_recovery`
    // (`app/launch/recovery.py`): it reads back the final ad's
    // deterministic remote object id, finds it (the write really
    // happened), resolves it `confirmed_succeeded`, and -- since every
    // other saga step already completed normally -- reconstructs the
    // parity check and resolves the whole manifest `succeeded`.
    const provisioned = (await waitForOperationState(
      page,
      WORKSPACE_SLUG,
      accepted.id,
      'controlled_mock',
      'succeeded',
      60_000
    )) as LaunchProvisioningResponse;
    expect(provisioned.provisioning_state).toBe('provisioned');
    await expect(
      launchSection(page).getByText('provisioned', { exact: true })
    ).toBeVisible({
      timeout: 15_000,
    });

    // No duplicate objects: recovery only ever reads
    // (`FakeMetaAdapter.read_object`), never re-creates
    // (`ensure_object`) -- exactly one campaign, two adsets, two ads.
    const objects = inspectFakeMetaObjects(
      seed.workspaceId,
      'controlled_mock',
      seed.controlledMockScenarioVersionId
    );
    expect(objects).toHaveLength(5);
    expect(
      objects.filter((object) => object.object_type === 'campaign')
    ).toHaveLength(1);
    expect(
      objects.filter((object) => object.object_type === 'adset')
    ).toHaveLength(2);
    expect(
      objects.filter((object) => object.object_type === 'ad')
    ).toHaveLength(2);
    const remoteIds = objects.map((object) => object.remote_object_id);
    expect(new Set(remoteIds).size).toBe(remoteIds.length);
  });

  test('Scenario 5a: API restart — durable reads recover, Worker completes independently', async ({
    page,
  }) => {
    test.setTimeout(300_000);

    // A multi-second pause on the very first saga step (`create_campaign`)
    // gives this test a window to restart the API mid-saga while the
    // Worker (a separate container/process) keeps running unaffected.
    const seed = seedPrerequisites({
      rules: [
        {
          object_type: 'campaign',
          match_ordinal: 1,
          effect: 'pause_before_apply',
          duration_ms: 8_000,
        },
      ],
    });
    const experimentName = `E2E launch recovery API restart ${seed.tag}`;

    const created = await createTwoCandidateExperiment(
      page,
      seed,
      experimentName
    );
    await openAndFreeze(page, created.id, experimentName);
    const { accepted } = await executeControlledMockLaunch(page, seed);

    // Restart the API server while the Worker is (most likely) still
    // inside its `time.sleep` for `create_campaign` -- Operation, Job, and
    // Provisioning state all live in PostgreSQL, never in the API
    // process's memory, so this restart loses nothing durable.
    await restartApiServer();

    // Polling resumes against the freshly-restarted API and observes the
    // same durable state the Worker (never restarted, never interrupted)
    // continues to advance independently.
    const provisioned = (await waitForOperationState(
      page,
      WORKSPACE_SLUG,
      accepted.id,
      'controlled_mock',
      'succeeded',
      120_000
    )) as LaunchProvisioningResponse;
    expect(provisioned.provisioning_state).toBe('provisioned');
    expect(provisioned.parity_check?.passed).toBe(true);

    const objects = inspectFakeMetaObjects(
      seed.workspaceId,
      'controlled_mock',
      seed.controlledMockScenarioVersionId
    );
    expect(objects).toHaveLength(5);
  });

  test('Scenario 5b: Worker restart after lease before dispatch — single execution, no duplicates', async ({
    page,
  }) => {
    test.setTimeout(180_000);

    // No fault needed -- this scenario is entirely about the Worker
    // crashing (simulated by `stopWorker`/`claimWorkerCheckpoint`) between
    // leasing the Job and ever calling `ensure_object`.
    const seed = seedPrerequisites();
    const experimentName = `E2E launch recovery worker lease crash ${seed.tag}`;

    const created = await createTwoCandidateExperiment(
      page,
      seed,
      experimentName
    );
    await openAndFreeze(page, created.id, experimentName);

    // Stop the Worker before Execute so the `launch_provisioning` Job the
    // execute command enqueues sits `queued`, unclaimed.
    stopWorker();
    const { accepted } = await executeControlledMockLaunch(page, seed);

    const queuedJobs = inspectJobsForOperation(
      seed.workspaceId,
      accepted.operation_id,
      'launch_provisioning'
    );
    expect(queuedJobs).toHaveLength(1);
    expect(queuedJobs[0].state).toBe('queued');
    const jobId = queuedJobs[0].id;

    // `worker_checkpoint.py`'s guarded one-shot claimant: leases exactly
    // this Job for 5s and exits *before* `mark_running` -- the crash
    // checkpoint a real Worker killed right after `claim_next` would leave
    // behind (state `leased`, no work ever started).
    const checkpoint = claimWorkerCheckpoint(jobId, 5);
    expect(checkpoint.error).toBeUndefined();
    expect(checkpoint.job_id).toBe(jobId);
    expect(checkpoint.state).toBe('leased');

    const leasedJobs = inspectJobsForOperation(
      seed.workspaceId,
      accepted.operation_id,
      'launch_provisioning'
    );
    expect(leasedJobs[0].state).toBe('leased');

    // Wait past the 5s lease so `marketai_claim_next_job_v2` treats it as
    // reclaimable again -- a `leased` (never `mark_running`) Job is always
    // safe to requeue (`app.platform.jobs.recover_expired_jobs`'s `row.state
    // == "leased"` branch), unlike a `running` Job past dispatch (Scenario
    // 5c below).
    await page.waitForTimeout(7_000);

    startWorker();

    const provisioned = (await waitForOperationState(
      page,
      WORKSPACE_SLUG,
      accepted.id,
      'controlled_mock',
      'succeeded',
      90_000
    )) as LaunchProvisioningResponse;
    expect(provisioned.provisioning_state).toBe('provisioned');

    // Exactly one execution: the real Worker claimed the Job a second time
    // (attempt 2 -- the checkpoint's claim was attempt 1) and ran the saga
    // exactly once; deterministic object ids make even an accidental
    // double-run idempotent, but this asserts it never happened at all.
    const finalJobs = inspectJobsForOperation(
      seed.workspaceId,
      accepted.operation_id,
      'launch_provisioning'
    );
    expect(finalJobs).toHaveLength(1);
    expect(finalJobs[0].state).toBe('succeeded');
    expect(finalJobs[0].attempt).toBe(2);

    const objects = inspectFakeMetaObjects(
      seed.workspaceId,
      'controlled_mock',
      seed.controlledMockScenarioVersionId
    );
    expect(objects).toHaveLength(5);
    const remoteIds = objects.map((object) => object.remote_object_id);
    expect(new Set(remoteIds).size).toBe(remoteIds.length);
  });

  test('Scenario 5c: Worker restart after dispatch → uncertain → reconciliation → failed', async ({
    page,
  }) => {
    // This scenario waits out the Worker's real Job lease
    // (`app/config.py`'s `JOB_LEASE_SECONDS`, default 300s) before
    // `recover_expired_jobs` can observe it expired -- the E2E stack must
    // configure a short `JOB_LEASE_SECONDS` for this to complete in
    // reasonable time. The timeout below assumes that configuration.
    test.setTimeout(420_000);

    // A long pause on `create_campaign` -- the saga's first step -- gives
    // this test a wide window to observe the dispatch marker and then kill
    // the Worker before the (never-returning) adapter call would resume.
    // `pause_before_apply` sleeps *before* the FakeMetaObjectRecord is
    // created, so a Worker killed mid-sleep never wrote the campaign at
    // all -- reconciliation's read-back must find nothing.
    const seed = seedPrerequisites({
      rules: [
        {
          object_type: 'campaign',
          match_ordinal: 1,
          effect: 'pause_before_apply',
          duration_ms: 60_000,
        },
      ],
    });
    const experimentName = `E2E launch recovery worker crash after dispatch ${seed.tag}`;

    const created = await createTwoCandidateExperiment(
      page,
      seed,
      experimentName
    );
    await openAndFreeze(page, created.id, experimentName);
    const { accepted } = await executeControlledMockLaunch(page, seed);

    // Wait for the durable dispatch marker
    // (`MetaCallIntentRecord.network_dispatched_at`) on the `create_campaign`
    // call -- `_execute_step` (saga.py) commits it before ever invoking the
    // adapter, so its presence proves the Worker is now inside the paused
    // `ensure_object` call.
    await waitForDispatchMarker(
      seed.workspaceId,
      accepted.operation_id,
      'POST',
      30_000
    );

    // Kill and restart the Worker mid-pause: the in-flight `ensure_object`
    // call is lost, its `MetaCallOutcomeRecord` never recorded, and the
    // Job's `running` state now has no live owner.
    restartWorker();

    // Once the Job's lease expires with no recorded outcome,
    // `recover_expired_jobs`'s `launch_provisioning` branch moves the
    // owning Operation straight to `uncertain` (no Python exception
    // handler ever ran in-process -- this is the durable, lease-expiry
    // path, distinct from Scenario 4's live in-process exception).
    const uncertain = (await waitForOperationState(
      page,
      WORKSPACE_SLUG,
      accepted.id,
      'controlled_mock',
      'uncertain',
      360_000
    )) as LaunchProvisioningResponse;

    const uncertainJob = inspectJobsForOperation(
      seed.workspaceId,
      accepted.operation_id,
      'launch_provisioning'
    );
    expect(uncertainJob[0].state).toBe('uncertain');
    expect(uncertainJob[0].error_code).toBe('paid_call_outcome_unknown');

    // Reconcile through the UI. Read-back finds no campaign object at all
    // (the write never happened), so `_derive_manifest_outcome` returns
    // `("failed", provisioning)` -- reconciliation resolves `failed`, not
    // `succeeded`/`partially_succeeded`.
    const reconcileButton = page.getByRole('button', {
      name: 'Reconcile uncertain operation',
    });
    await expect(reconcileButton).toBeVisible();
    const [recoveryResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${LAUNCH_BFF}/operations/${uncertain.operation_id}/recovery`
      ),
      reconcileButton.click(),
    ]);
    const recovery = await readJson<LaunchRecoveryResponse>(recoveryResponse);
    expect(recovery.original_operation_id).toBe(uncertain.operation_id);

    const failed = (await waitForOperationState(
      page,
      WORKSPACE_SLUG,
      accepted.id,
      'controlled_mock',
      'failed',
      60_000
    )) as LaunchProvisioningResponse;
    expect(failed.provisioning_state).toBe('failed');
    await expect(
      launchSection(page).getByText('failed', { exact: true })
    ).toBeVisible({
      timeout: 15_000,
    });

    // Nothing was ever created for this scenario's ScenarioVersion.
    const objects = inspectFakeMetaObjects(
      seed.workspaceId,
      'controlled_mock',
      seed.controlledMockScenarioVersionId
    );
    expect(objects).toHaveLength(0);
  });

  test('Scenario 9: definitive parity failure → Operation and Job both terminal-failed', async ({
    page,
  }) => {
    test.setTimeout(180_000);

    // `return_state_patch` mutates the first AdSet's applied state after
    // creation -- the saga's own writes succeed and complete normally, but
    // the post-provisioning readback/parity check sees a budget that no
    // longer matches the frozen preview.
    const seed = seedPrerequisites({
      rules: [
        {
          object_type: 'adset',
          match_ordinal: 1,
          effect: 'return_state_patch',
          state_patch: { daily_budget_minor: 1 },
        },
      ],
    });
    const experimentName = `E2E launch recovery parity failure ${seed.tag}`;

    const created = await createTwoCandidateExperiment(
      page,
      seed,
      experimentName
    );
    await openAndFreeze(page, created.id, experimentName);
    const { accepted } = await executeControlledMockLaunch(page, seed);

    // The saga completes every write and readback (no exception, no
    // uncertainty), but `execute_provisioning`'s parity check fails on
    // `budget_ok` -- a *definitive*, not uncertain, terminal failure.
    const failed = (await waitForOperationState(
      page,
      WORKSPACE_SLUG,
      accepted.id,
      'controlled_mock',
      'failed',
      90_000
    )) as LaunchProvisioningResponse;
    expect(failed.provisioning_state).toBe('failed');
    expect(failed.parity_check?.passed).toBe(false);
    expect(failed.parity_check?.budget_parity_verified).toBe(false);
    expect(failed.parity_check?.all_candidates_present).toBe(true);
    expect(failed.parity_check?.config_parity_verified).toBe(true);
    // `state_patch` may never touch `status` (the adapter enforces this),
    // so every object is still PAUSED -- only budget parity is violated.
    expect(failed.parity_check?.status_parity_verified).toBe(true);

    await expect(
      launchSection(page).getByText('failed', { exact: true })
    ).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByText(/Parity check: failed/)).toBeVisible();

    // The Job is terminal-`failed`, not `retry_wait` -- `worker_runtime`
    // calls `jobs.fail(..., retryable=False)` for a `ProvisioningError`,
    // which `jobs.fail` always resolves to terminal `state = "failed"`
    // regardless of remaining attempts.
    const jobRows = inspectJobsForOperation(
      seed.workspaceId,
      accepted.operation_id,
      'launch_provisioning'
    );
    expect(jobRows).toHaveLength(1);
    expect(jobRows[0].state).toBe('failed');
    expect(jobRows[0].error_code).toBe('launch_provisioning_failed');

    // No "Reconcile uncertain operation" control -- this Operation was
    // never uncertain, it is definitively failed.
    await expect(
      page.getByRole('button', { name: 'Reconcile uncertain operation' })
    ).toHaveCount(0);
  });
});
