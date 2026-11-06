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
import { inspectFakeMetaObjects, waitForOperationState } from './helpers';
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
type ProblemDetails = components['schemas']['ProblemDetails'];

/**
 * Real-backend E2E coverage for issue #88's Scenario 6 (authorization) and
 * Scenario 7 (mode contamination prevention). Consumes the Task 10
 * infrastructure (`fixtures.ts`'s `seedPrerequisites`, `helpers.ts`'s
 * `inspectFakeMetaObjects`/`waitForOperationState`) and drives every step
 * through the real, rendered `ExperimentsWorkspace` UI against a live
 * PostgreSQL/FastAPI/Worker stack, exactly like `duplicate-resolution.spec.ts`
 * (Task 11) and `launch-recovery.spec.ts` (Task 12).
 *
 * This suite never intercepts `/api/domain/**` or manufactures Experiment,
 * launch-preview, or provisioning state in browser memory. It cannot run
 * without a live Postgres/API/Worker stack; it is written to be structurally
 * correct and type-safe against the real OpenAPI contract
 * (`lib/domain-client/generated/domain-v1.d.ts`) and the real
 * component/service source, not to be executed in this sandbox.
 */

const LAUNCH_BFF = `/api/domain/v1/w/${WORKSPACE_SLUG}/launch`;
const LAUNCH_ACK_TEXT_PREFIX =
  'I reviewed every exact candidate AssetVersion and creative configuration';
const EXECUTE_ACK_TEXT_PREFIX = 'Execute this exact';

/**
 * The pre-authenticated `member` Playwright storage state
 * (`e2e/workspace/fixtures.ts`'s `setup` project) -- a user who is MEMBER
 * (not OWNER) of the same shared E2E Workspace `seedPrerequisites()` seeds
 * into. `e2e/localization/authorization.spec.ts` uses this exact same
 * credential the same way (a fresh `browser.newContext({ storageState })`
 * against the ambient owner-scoped `WORKSPACE_SLUG`) to prove Member access
 * within the shared Workspace.
 */
const MEMBER_STORAGE_STATE = 'e2e/.auth/member.json';

/**
 * `workspace-b` is this repo's established "foreign Workspace" fixture: the
 * default `owner.json` Playwright session
 * (`e2e/workspace/fixtures.ts`/`playwright.config.ts`'s `chromium` project)
 * has no membership there -- see `e2e/assets/cross-workspace.spec.ts` and
 * `e2e/workspace/owner-member.spec.ts`, which assert exactly this same
 * no-membership boundary against the same slug. Unlike those, an Experiments
 * BFF resource read/write for an id outside the caller's Workspace resolves
 * `404` (RLS scopes it out entirely), the same pattern
 * `assets/cross-workspace.spec.ts`'s asset-version-detail check documents --
 * not the `200` + "Access Denied" props pattern `getWorkspacePageProps()`
 * uses for a bare page navigation.
 */
const FOREIGN_WORKSPACE_SLUG = 'workspace-b';

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

/** A draft Experiment with exactly two, non-duplicate entries -- the same
 * simplest combination `launch-recovery.spec.ts` and
 * `judgement-winner.spec.ts` use, since duplicate resolution is orthogonal
 * to both authorization and mode contamination. */
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
  return frozen;
}

async function bindCandidateThroughUi(
  page: Page,
  candidatePosition: number,
  adAccountId: string,
  adId: string
): Promise<void> {
  const label = candidatePosition + 1;
  const accountInput = page.getByLabel(`Ad account for candidate ${label}`);
  await accountInput.fill(adAccountId);
  await page.getByLabel(`Ad ID for candidate ${label}`).fill(adId);
  await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        /\/candidates\/.+\/bind$/.test(new URL(response.url()).pathname)
    ),
    accountInput.locator('xpath=following-sibling::button').click(),
  ]);
}

/** The `<section>` carrying the "PAUSED launch preview" `<h3>`. */
function launchSection(page: Page) {
  return page
    .getByRole('heading', { name: 'PAUSED launch preview' })
    .locator('xpath=ancestor::section');
}

test.describe('Authorization and mode contamination', () => {
  test('Scenario 6: Member and Owner can complete lifecycle; cross-workspace access is rejected', async ({
    page,
    browser,
  }) => {
    test.setTimeout(180_000);
    const seed = seedPrerequisites();

    // 1. Member completes the full draft -> freeze -> bind -> start ->
    //    judge lifecycle in the shared Workspace. "The existing model grants
    //    both Owner and Member access to all experiment operations" (spec
    //    §Scenario 6.2) -- there is no Owner-only gate anywhere in this
    //    lifecycle (unlike Localization's `withdraw_library_version` gate),
    //    so this is a plain "does every command succeed for a Member"
    //    check, not a positive/negative authorization pair.
    const memberContext = await browser.newContext({
      storageState: MEMBER_STORAGE_STATE,
    });
    try {
      const memberPage = await memberContext.newPage();
      const memberExperimentName = `E2E member lifecycle ${seed.tag}`;
      const memberCreated = await createTwoCandidateExperiment(
        memberPage,
        seed,
        memberExperimentName
      );
      expect(memberCreated.entries).toHaveLength(2);

      const memberFrozen = await openAndFreeze(
        memberPage,
        memberCreated.id,
        memberExperimentName
      );
      const memberCandidates = memberFrozen.candidates ?? [];
      expect(memberCandidates).toHaveLength(2);
      for (let index = 0; index < memberCandidates.length; index++) {
        const candidate = memberCandidates[index];
        await bindCandidateThroughUi(
          memberPage,
          candidate.position,
          `act_${seed.tag}_member`,
          `ad_member_${seed.tag}_${index}`
        );
      }

      const [memberStartResponse] = await Promise.all([
        memberPage.waitForResponse(
          (response) =>
            response.request().method() === 'POST' &&
            new URL(response.url()).pathname ===
              `${EXPERIMENTS_BFF}/${memberCreated.id}/start`
        ),
        memberPage.getByRole('button', { name: 'Start measurement' }).click(),
      ]);
      const memberRunning =
        await readJson<ExperimentDetail>(memberStartResponse);
      expect(memberRunning.state).toBe('running');

      const [memberJudgeResponse] = await Promise.all([
        memberPage.waitForResponse(
          (response) =>
            response.request().method() === 'POST' &&
            new URL(response.url()).pathname ===
              `${EXPERIMENTS_BFF}/${memberCreated.id}/judgements`
        ),
        memberPage.getByRole('button', { name: 'Run judgement' }).click(),
      ]);
      expect(memberJudgeResponse.ok()).toBe(true);
    } finally {
      await memberContext.close();
    }

    // 2. Owner completes the same create -> freeze step in their own
    //    Workspace. The decisive full golden path (Launch through Confirmed
    //    Winner) for the Owner session is already exhaustively covered by
    //    `duplicate-resolution.spec.ts`'s Scenario 1 -- re-running it here
    //    would only re-prove business logic this spec isn't about, so this
    //    checks just enough (create, analyze, freeze) to establish Owner
    //    parity before the cross-workspace check below reuses this same
    //    Experiment id.
    const ownerExperimentName = `E2E owner lifecycle ${seed.tag}`;
    const ownerCreated = await createTwoCandidateExperiment(
      page,
      seed,
      ownerExperimentName
    );
    const ownerFrozen = await openAndFreeze(
      page,
      ownerCreated.id,
      ownerExperimentName
    );
    expect(ownerFrozen.state).toBe('frozen');

    // 3. Cross-Workspace access attempt: the Owner session has no
    //    membership in `workspace-b`. Both the (would-be) analysis read and
    //    the freeze write resolve `404` -- RLS scopes the experiment id out
    //    of that Workspace entirely rather than leaking a `403` that would
    //    confirm the id exists somewhere else (same rationale
    //    `assets/cross-workspace.spec.ts` documents for asset-version reads).
    const foreignAnalysis = await page.request.get(
      `/api/domain/v1/w/${FOREIGN_WORKSPACE_SLUG}/experiments/${ownerCreated.id}/duplicate-analysis`
    );
    expect(foreignAnalysis.status()).toBe(404);

    const foreignFreeze = await page.request.post(
      `/api/domain/v1/w/${FOREIGN_WORKSPACE_SLUG}/experiments/${ownerCreated.id}/freeze`,
      {
        headers: await domainMutationHeaders(page),
        data: {
          expected_revision: ownerCreated.revision,
          analyzed_candidate_set_hash: 'foreign-workspace-probe',
          concept_resolution_plan: [],
        },
      }
    );
    expect(foreignFreeze.status()).toBe(404);

    // 4. A foreign Workspace identifier in the page's own URL is also
    //    rejected server-side, not client-side -- `getWorkspacePageProps()`
    //    (`lib/workspace-bootstrap.ts`) still returns HTTP 200 with
    //    "Access Denied" props for a no-membership page load (the same
    //    pattern `e2e/assets/cross-workspace.spec.ts` and
    //    `e2e/workspace/owner-member.spec.ts` assert for other domains), so
    //    this checks that shape rather than an unverified 403/404 on the
    //    page navigation itself.
    const foreignPageResponse = await page.goto(
      `/w/${FOREIGN_WORKSPACE_SLUG}/legacy-experiments?experiment=${ownerCreated.id}`
    );
    expect(foreignPageResponse?.status()).toBe(200);
    await expect(page.getByText('Access Denied')).toBeVisible();
    await expect(
      page.getByRole('heading', { name: ownerExperimentName })
    ).toHaveCount(0);
  });

  test('Scenario 7: mode contamination prevention', async ({ page }) => {
    test.setTimeout(240_000);
    const seed = seedPrerequisites();

    // 1. Golden-path Launch (mirroring Scenario 1's `customer_sandbox` path,
    //    reduced to just what mode-tagging needs): every FakeMetaAdapter
    //    object this creates must be tagged `customer_sandbox`.
    const sandboxExperimentName = `E2E mode sandbox ${seed.tag}`;
    const sandboxCreated = await createTwoCandidateExperiment(
      page,
      seed,
      sandboxExperimentName
    );
    await openAndFreeze(page, sandboxCreated.id, sandboxExperimentName);

    await page
      .getByLabel('Sandbox Meta connection ID')
      .fill(seed.customerSandboxConnectionId);
    await page
      .getByLabel('Sandbox Meta ad account ID')
      .fill(seed.customerSandboxAdAccountId);
    await expect(
      page.getByText('Meta Sandbox · customer_sandbox')
    ).toBeVisible();

    const [sandboxPreviewResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname === `${LAUNCH_BFF}/previews`
      ),
      page.getByRole('button', { name: 'Create Sandbox preview' }).click(),
    ]);
    const sandboxPreview = await readJson<MultiCandidatePreviewResponse>(
      sandboxPreviewResponse
    );
    expect(sandboxPreview.meta_mode).toBe('customer_sandbox');

    await page.getByText(LAUNCH_ACK_TEXT_PREFIX, { exact: false }).click();
    const [sandboxConfirmResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${LAUNCH_BFF}/previews/${sandboxPreview.id}/confirm`
      ),
      page.getByRole('button', { name: 'Confirm Sandbox preview' }).click(),
    ]);
    const sandboxConfirmed = await readJson<MultiCandidateConfirmationResponse>(
      sandboxConfirmResponse
    );
    expect(sandboxConfirmed.state).toBe('confirmed');

    await launchSection(page)
      .getByText(EXECUTE_ACK_TEXT_PREFIX, { exact: false })
      .click();
    const [sandboxExecuteResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${LAUNCH_BFF}/previews/${sandboxPreview.id}/execute`
      ),
      page
        .getByRole('button', { name: 'Execute PAUSED Sandbox launch' })
        .click(),
    ]);
    const sandboxAccepted = await readJson<LaunchProvisioningAccepted>(
      sandboxExecuteResponse
    );
    const sandboxProvisioned = (await waitForOperationState(
      page,
      WORKSPACE_SLUG,
      sandboxAccepted.id,
      'customer_sandbox',
      'succeeded',
      60_000
    )) as LaunchProvisioningResponse;
    expect(sandboxProvisioned.provisioning_state).toBe('provisioned');

    const sandboxObjects = inspectFakeMetaObjects(
      seed.workspaceId,
      'customer_sandbox',
      seed.customerSandboxScenarioVersionId
    );
    expect(sandboxObjects.length).toBeGreaterThan(0);
    for (const object of sandboxObjects) {
      expect((object.state as Record<string, unknown>).status).toBe('PAUSED');
    }

    // Cross-mode DB tagging boundary: no row can ever exist tagged
    // `controlled_mock` for this `customer_sandbox`-only ScenarioVersion --
    // ADR 0009's composite `(workspace_id, meta_mode, scenario_version_id)`
    // provenance is enforced at the schema level, so this must return empty
    // rather than accidentally aliasing the sandbox evidence into the mock
    // mode's read.
    const crossTaggedAsMock = inspectFakeMetaObjects(
      seed.workspaceId,
      'controlled_mock',
      seed.customerSandboxScenarioVersionId
    );
    expect(crossTaggedAsMock).toHaveLength(0);

    // 2. Negative test: a Launch request specifying `controlled_mock` mode
    //    cannot cross the `customer_sandbox` evidence boundary. `create_preview`
    //    (`app/launch/service.py`) looks up the submitted `connection_id`
    //    filtered on `MetaConnectionRecord.meta_mode == context.meta_mode`
    //    (line ~360) -- a `customer_sandbox` connection id submitted under
    //    `controlled_mock` mode matches no row and raises `LaunchNotFound`
    //    (-> HTTP 404 via `_multi_launch_error_status`).
    //    The provisioned sandbox Launch hides "Create fresh preview", so
    //    reopen the experiment without its launch route first.
    await page.goto(`${EXPERIMENTS_URL}?experiment=${sandboxCreated.id}`);
    await expectFrozenPanel(page);
    await page.locator('#meta-mode-select').selectOption('controlled_mock');
    await expect(
      page.getByText('Meta Sandbox · controlled_mock')
    ).toBeVisible();
    await page
      .getByLabel('Sandbox Meta connection ID')
      .fill(seed.customerSandboxConnectionId);
    await page
      .getByLabel('Sandbox Meta ad account ID')
      .fill(seed.customerSandboxAdAccountId);
    const [crossModePreviewResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname === `${LAUNCH_BFF}/previews`
      ),
      page.getByRole('button', { name: 'Create Sandbox preview' }).click(),
    ]);
    expect(crossModePreviewResponse.ok()).toBe(false);
    expect(crossModePreviewResponse.status()).toBe(404);

    // ... and the reverse: a `controlled_mock` connection id submitted under
    // `customer_sandbox` mode is equally rejected.
    await page.locator('#meta-mode-select').selectOption('customer_sandbox');
    await page
      .getByLabel('Sandbox Meta connection ID')
      .fill(seed.controlledMockConnectionId);
    const [reverseCrossModeResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname === `${LAUNCH_BFF}/previews`
      ),
      page.getByRole('button', { name: 'Create Sandbox preview' }).click(),
    ]);
    expect(reverseCrossModeResponse.ok()).toBe(false);
    expect(reverseCrossModeResponse.status()).toBe(404);

    // 3. `internal_validation` cannot even be selected from this
    //    customer-Workspace UI -- the "Evidence mode" select
    //    (`#meta-mode-select` in `ExperimentsWorkspace.tsx`) only ever
    //    renders `customer_sandbox`/`controlled_mock` options. Exercising
    //    real `internal_validation` isolation (a Platform Workspace,
    //    platform-operator JWT/JWKS -- ADR 0009) needs infrastructure this
    //    suite's `seed_prerequisites.py` does not provide and is out of
    //    scope here.
    await expect(page.locator('#meta-mode-select option')).toHaveCount(2);
    await expect(
      page.locator('#meta-mode-select option[value="internal_validation"]')
    ).toHaveCount(0);

    // 4. `controlled_mock` evidence is decision-ineligible: drive a second,
    //    independent experiment through a real (fault-free) `controlled_mock`
    //    Launch, then bind its candidates to the exact `ad` FakeMetaObject
    //    ids that Launch just durably created. No live Meta sync ever runs
    //    in this suite, so -- exactly as in `judgement-winner.spec.ts`'s
    //    Scenario 3 -- zero `AdDailyMetricRecord` rows exist for those ids;
    //    `judge()` returns `inconclusive` for every entry, and
    //    `confirm_proposal_v2` (`app/experiments/service.py`) rejects
    //    confirmation outright once every proposed entry is inconclusive
    //    (`has_material` check -> `no_material_verdicts`, HTTP 409). There is
    //    therefore no code path by which this `controlled_mock` evidence
    //    could ever become a confirmed Winner.
    const mockExperimentName = `E2E mode mock decision-ineligible ${seed.tag}`;
    const mockCreated = await createTwoCandidateExperiment(
      page,
      seed,
      mockExperimentName
    );
    const mockFrozen = await openAndFreeze(
      page,
      mockCreated.id,
      mockExperimentName
    );

    await page.locator('#meta-mode-select').selectOption('controlled_mock');
    await page
      .getByLabel('Sandbox Meta connection ID')
      .fill(seed.controlledMockConnectionId);
    await page
      .getByLabel('Sandbox Meta ad account ID')
      .fill(seed.controlledMockAdAccountId);
    const [mockPreviewResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname === `${LAUNCH_BFF}/previews`
      ),
      page.getByRole('button', { name: 'Create Sandbox preview' }).click(),
    ]);
    const mockPreview =
      await readJson<MultiCandidatePreviewResponse>(mockPreviewResponse);
    expect(mockPreview.meta_mode).toBe('controlled_mock');

    await page.getByText(LAUNCH_ACK_TEXT_PREFIX, { exact: false }).click();
    const [mockConfirmResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${LAUNCH_BFF}/previews/${mockPreview.id}/confirm`
      ),
      page.getByRole('button', { name: 'Confirm Sandbox preview' }).click(),
    ]);
    expect(mockConfirmResponse.ok()).toBe(true);

    await launchSection(page)
      .getByText(EXECUTE_ACK_TEXT_PREFIX, { exact: false })
      .click();
    const [mockExecuteResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${LAUNCH_BFF}/previews/${mockPreview.id}/execute`
      ),
      page
        .getByRole('button', { name: 'Execute PAUSED Sandbox launch' })
        .click(),
    ]);
    const mockAccepted =
      await readJson<LaunchProvisioningAccepted>(mockExecuteResponse);
    const mockProvisioned = (await waitForOperationState(
      page,
      WORKSPACE_SLUG,
      mockAccepted.id,
      'controlled_mock',
      'succeeded',
      60_000
    )) as LaunchProvisioningResponse;
    expect(mockProvisioned.provisioning_state).toBe('provisioned');

    const mockAdObjects = inspectFakeMetaObjects(
      seed.workspaceId,
      'controlled_mock',
      seed.controlledMockScenarioVersionId
    ).filter((object) => object.object_type === 'ad');
    expect(mockAdObjects).toHaveLength(2);

    const mockCandidates = mockFrozen.candidates ?? [];
    expect(mockCandidates).toHaveLength(2);
    for (let index = 0; index < mockCandidates.length; index++) {
      const candidate = mockCandidates[index];
      await bindCandidateThroughUi(
        page,
        candidate.position,
        seed.controlledMockAdAccountId,
        mockAdObjects[index].remote_object_id
      );
    }

    const [mockStartResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${EXPERIMENTS_BFF}/${mockCreated.id}/start`
      ),
      page.getByRole('button', { name: 'Start measurement' }).click(),
    ]);
    const mockRunning = await readJson<ExperimentDetail>(mockStartResponse);
    expect(mockRunning.state).toBe('running');

    const [mockJudgeResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${EXPERIMENTS_BFF}/${mockCreated.id}/judgements`
      ),
      page.getByRole('button', { name: 'Run judgement' }).click(),
    ]);
    const mockJudgement = await readJson<{
      proposal_id?: string | null;
      proposal_state?: string | null;
    }>(mockJudgeResponse);
    expect(mockJudgement.proposal_state).toBe('created');
    expect(mockJudgement.proposal_id).toBeTruthy();

    // No downstream Winner is reachable through the UI (the confirm button
    // is disabled once every proposed entry is inconclusive -- see
    // `judgement-winner.spec.ts`'s Scenario 3), and a direct attempt at the
    // real confirmation command is rejected server-side.
    const proposalCard = page.locator('article').filter({
      has: page.getByRole('heading', { name: 'Judgement evidence' }),
    });
    await expect(
      proposalCard.getByRole('button', { name: 'No Winner to confirm' })
    ).toBeDisabled();

    const mockAfterJudgement = await readJson<ExperimentDetail>(
      await page.request.get(`${EXPERIMENTS_BFF}/${mockCreated.id}`)
    );
    const mockProposal = (mockAfterJudgement.proposals ?? []).find(
      (proposal) => proposal.id === mockJudgement.proposal_id
    );
    expect(mockProposal).toBeDefined();

    const directConfirmResponse = await page.request.post(
      `${EXPERIMENTS_BFF}/${mockCreated.id}/proposals/${mockJudgement.proposal_id}/confirm`,
      {
        headers: await domainMutationHeaders(page),
        data: { expected_proposal_sequence: mockProposal!.sequence },
      }
    );
    expect(directConfirmResponse.ok()).toBe(false);
    expect(directConfirmResponse.status()).toBe(409);
    const confirmProblem =
      (await directConfirmResponse.json()) as ProblemDetails;
    // An `ExperimentConflict` whose message is a code carries it as the
    // response `code` (#253): `confirm_proposal_v2`'s `raise
    // ExperimentConflict("no_material_verdicts")` surfaces as
    // `code: "no_material_verdicts"`; `detail` is only a diagnostic.
    expect(confirmProblem.code).toBe('no_material_verdicts');

    await expect(
      page.getByRole('heading', { name: 'Confirmed decisions' })
    ).toHaveCount(0);
  });
});
