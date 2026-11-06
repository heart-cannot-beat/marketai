import { execFileSync } from 'child_process';
import path from 'path';
import { expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import {
  test,
  WORKSPACE_SLUG,
  EXPERIMENTS_URL,
  EXPERIMENTS_BFF,
  seedPrerequisites,
  readJson,
  inspectFakeMetaObjects,
  waitForOperationState,
  domainMutationHeaders,
} from './fixtures';
import type { ExperimentsPrerequisites } from './fixtures';
import type { components } from '@/lib/domain-client/generated/domain-v1';

type ExperimentDetail = components['schemas']['ExperimentDetail'];
type ExperimentEntryMutation = components['schemas']['ExperimentEntryMutation'];
type DuplicateAnalysisResponse =
  components['schemas']['DuplicateAnalysisResponse'];
type DuplicateGroup = components['schemas']['DuplicateGroup'];
type MultiCandidatePreviewResponse =
  components['schemas']['MultiCandidatePreviewResponse'];
type MultiCandidateConfirmationResponse =
  components['schemas']['MultiCandidateConfirmationResponse'];
type LaunchProvisioningAccepted =
  components['schemas']['LaunchProvisioningAccepted'];
type LaunchProvisioningResponse =
  components['schemas']['LaunchProvisioningResponse'];
type ExperimentJudgementResult =
  components['schemas']['ExperimentJudgementResult'];
type ExperimentConfirmationResult =
  components['schemas']['ExperimentConfirmationResult'];
type ProblemDetails = components['schemas']['ProblemDetails'];
type AssetVersionDetail = components['schemas']['AssetVersionDetail'];

/**
 * Real-backend E2E coverage for issue #88's duplicate-resolution scenarios
 * (SDD Scenario 1, 2a, 8). Consumes the Task 10 infrastructure --
 * `fixtures.ts` (`seedPrerequisites`, `WORKSPACE_SLUG`/`EXPERIMENTS_BFF`),
 * `helpers.ts` (`inspectFakeMetaObjects`, `waitForOperationState`) -- and
 * drives every duplicate-resolution / freeze / launch / judgement /
 * confirmation step through the real, rendered `ExperimentsWorkspace` UI
 * (`web/components/experiments/ExperimentsWorkspace.tsx`,
 * `DuplicateResolutionPanel.tsx`, `FrozenResolutionEvidence.tsx`) against a
 * live PostgreSQL/FastAPI/Worker stack. Only pre-UI setup (experiment +
 * entry creation, and the out-of-band entry mutation Scenario 2a requires)
 * goes through direct `page.request` calls, exactly like
 * `seedPrerequisites` seeds pre-UI database rows -- this suite never
 * intercepts `/api/domain/**` or manufactures duplicate-group/freeze/launch
 * state in browser memory.
 *
 * These tests cannot run without a live Postgres/API/Worker stack (see
 * `seed_prerequisites.py`'s required environment). They are written to be
 * structurally correct and type-safe against the real OpenAPI contract
 * (`lib/domain-client/generated/domain-v1.d.ts`) and the real component
 * source, not to be executed in this sandbox.
 */

const LAUNCH_BFF = `/api/domain/v1/w/${WORKSPACE_SLUG}/launch`;
const FREEZE_ACK_TEXT =
  'I reviewed the candidates, budget, schedule, and immutable context.';

/** Mirrors `toDraftContext` in `ExperimentsWorkspace.tsx` and satisfies
 * `app.experiments.service._validate_draft_context`'s schema-2 rules
 * (7-day click / 1-day view attribution is fixed, ctr metric, etc). */
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
 * Creates a draft Experiment and adds every `assetVersionIds` entry through
 * the real `create_experiment`/`add_experiment_entry` command path (direct
 * `page.request` calls, sharing the authenticated owner session's cookies
 * with the browser tab). Returns the persisted `ExperimentDetail` so the
 * caller has exact entry ids/positions before ever opening the UI.
 */
async function createDraftExperiment(
  page: Page,
  seed: ExperimentsPrerequisites,
  name: string,
  assetVersionIds: string[]
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
  for (const assetVersionId of assetVersionIds) {
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

/** The freeze-acknowledgement checkbox in the "Next action" sidebar
 * (`ExperimentsWorkspace.tsx`'s draft-state panel), located by its label
 * text since it carries no id/aria-label of its own. */
function freezeAckCheckbox(page: Page) {
  return page
    .locator('label')
    .filter({ hasText: FREEZE_ACK_TEXT })
    .locator('input[type="checkbox"]');
}

/**
 * Resolves one `DuplicateResolutionPanel` group card: selects the action
 * radio (`input[name="action-<hash>"]`), fills the affected-candidate /
 * remap-target selects when the action needs them, and fills the required
 * reason textarea -- exactly the fields `DuplicateResolutionPanel.tsx`
 * renders per `group.duplicate_group_hash`.
 */
async function resolveDuplicateGroup(
  page: Page,
  group: DuplicateGroup,
  action:
    | 'allow_controlled_duplicate'
    | 'exclude_source_candidate'
    | 'remap_before_freeze',
  reason: string,
  options?: { affectedCandidateFingerprint?: string; remapTargetId?: string }
): Promise<void> {
  const hash = group.duplicate_group_hash;
  await page.locator(`input[name="action-${hash}"][value="${action}"]`).check();
  if (options?.affectedCandidateFingerprint) {
    await page
      .locator(`#affected-candidate-${hash}`)
      .selectOption(options.affectedCandidateFingerprint);
  }
  if (options?.remapTargetId) {
    await page
      .locator(`#remap-target-${hash}`)
      .selectOption(options.remapTargetId);
  }
  await page.locator(`#reason-${hash}`).fill(reason);
}

/**
 * Binds one frozen candidate through the "Attach read-only Meta evidence"
 * card (`ExperimentsWorkspace.tsx`), using the candidate's rendered
 * `candidate.position` (0-based) to find its "Ad account for candidate
 * N+1"/"Ad ID for candidate N+1" inputs and adjacent "Bind" button.
 */
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

/**
 * Seeds `AdDailyMetricRecord` rows for bound candidates through
 * `seed_ad_metrics.py` (co-located with this spec -- see that file's
 * header for why: `run_judgement_v2` reads real Meta-sync metrics rows and
 * this suite never runs a live Meta sync). Every row lands on or before
 * `judge_experiment`'s default cutoff (`today - 1 day`), so the very next
 * "Run judgement" click sees every seeded day.
 */
function seedAdMetrics(
  seed: ExperimentsPrerequisites,
  candidates: Array<{
    adAccountId: string;
    adId: string;
    dailyImpressions: number;
    dailyClicks: number;
  }>,
  days = 7
): { seeded_rows: number; start_day: string; end_day: string } {
  const repositoryRoot = path.resolve(__dirname, '..', '..', '..');
  const python = process.env.E2E_PYTHON ?? 'python3';
  const script = path.join(__dirname, 'seed_ad_metrics.py');
  const raw = execFileSync(
    python,
    [
      script,
      '--workspace-id',
      seed.workspaceId,
      '--days',
      String(days),
      '--candidates-json',
      JSON.stringify(
        candidates.map((candidate) => ({
          ad_account_id: candidate.adAccountId,
          ad_id: candidate.adId,
          daily_impressions: candidate.dailyImpressions,
          daily_clicks: candidate.dailyClicks,
        }))
      ),
    ],
    {
      cwd: repositoryRoot,
      env: { ...process.env, PYTHONPATH: repositoryRoot },
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'inherit'],
    }
  );
  return JSON.parse(raw.trim()) as {
    seeded_rows: number;
    start_day: string;
    end_day: string;
  };
}

test.describe('Experiments duplicate resolution', () => {
  test.describe.configure({ mode: 'serial' });

  test('Scenario 1: golden path — resolve → freeze → Launch → judgement → Winner', async ({
    page,
  }) => {
    test.setTimeout(300_000);
    const seed = seedPrerequisites();
    const experimentName = `E2E golden path ${seed.tag}`;

    // 1. Seed an experiment whose entries include the seed's duplicate pair
    //    (shared concept key) plus the independent, non-duplicate candidate.
    const created = await createDraftExperiment(page, seed, experimentName, [
      seed.duplicatePairAssetVersionIds[0],
      seed.duplicatePairAssetVersionIds[1],
      seed.independentAssetVersionId,
    ]);
    expect(created.entries).toHaveLength(3);

    // 2. Navigate to the experiment.
    await page.goto(`${EXPERIMENTS_URL}?experiment=${created.id}`);
    await expect(
      page.getByRole('heading', { name: experimentName })
    ).toBeVisible();

    // 3. The real duplicate analysis must surface exactly the one
    //    shared-concept group the seed guarantees.
    await expect(
      page.getByRole('heading', { name: 'Duplicate Concept Resolution' })
    ).toBeVisible({ timeout: 30_000 });
    const analysis = await readJson<DuplicateAnalysisResponse>(
      await page.request.get(
        `${EXPERIMENTS_BFF}/${created.id}/duplicate-analysis`
      )
    );
    expect(analysis.has_duplicates).toBe(true);
    expect(analysis.duplicate_groups).toHaveLength(1);
    const group = analysis.duplicate_groups![0];
    expect(group.members).toHaveLength(2);
    expect(group.concept_key_id).toBe(seed.sharedConceptKeyId);

    // 4. Resolve the group. The seed only ever produces one shared-concept
    //    pair, so this exercises `allow_controlled_duplicate` -- the other
    //    two allowed actions (`exclude_source_candidate`,
    //    `remap_before_freeze`) are exercised in the duplicate-group-action
    //    unit/integration coverage, not here, since a single seeded group
    //    can only carry one resolution.
    const freezeReason =
      'Both creative variants stay in market; duplicate concept is intentional for this test.';
    await resolveDuplicateGroup(
      page,
      group,
      'allow_controlled_duplicate',
      freezeReason
    );

    // 5. Acknowledge and freeze.
    await freezeAckCheckbox(page).check();
    const [freezeResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${EXPERIMENTS_BFF}/${created.id}/freeze`
      ),
      page.getByRole('button', { name: 'Freeze experiment' }).click(),
    ]);
    const frozen = await readJson<ExperimentDetail>(freezeResponse);
    expect(frozen.state).toBe('frozen');
    expect(frozen.concept_resolution_evidence?.had_duplicates).toBe(true);
    expect(frozen.concept_resolution_evidence?.resolutions).toHaveLength(1);
    expect(frozen.concept_resolution_evidence?.resolutions?.[0].action).toBe(
      'allow_controlled_duplicate'
    );
    expect(frozen.candidates).toHaveLength(3);

    // 6. Frozen evidence must be visible, and survive a refresh.
    await expect(
      page.getByRole('heading', { name: 'Concept Resolution' })
    ).toBeVisible();
    await expect(page.getByText(freezeReason, { exact: false })).toBeVisible();
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'Concept Resolution' })
    ).toBeVisible();
    await expect(page.getByText(freezeReason, { exact: false })).toBeVisible();

    // 7. Preview Launch (customer_sandbox) against the real FakeMetaAdapter.
    const launchSection = page
      .getByRole('heading', { name: 'PAUSED launch preview' })
      .locator('xpath=ancestor::section');
    await page
      .getByLabel('Sandbox Meta connection ID')
      .fill(seed.customerSandboxConnectionId);
    await page
      .getByLabel('Sandbox Meta ad account ID')
      .fill(seed.customerSandboxAdAccountId);
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
    expect(preview.candidate_count).toBe(3);
    expect(preview.state).toBe('draft');
    await expect(page).toHaveURL(
      new RegExp(`[?&]launchPreview=${preview.id}(?:&|$)`)
    );

    // 8. Confirm Launch.
    await page
      .getByText(
        'I reviewed every exact candidate AssetVersion and creative configuration',
        {
          exact: false,
        }
      )
      .click();
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
    await expect(
      launchSection.getByText(
        'Preview confirmed. No object was created by preview confirmation.'
      )
    ).toBeVisible();

    await launchSection.getByText(/^Execute this exact/).click();
    const [executeResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${LAUNCH_BFF}/previews/${preview.id}/execute`
      ),
      page
        .getByRole('button', { name: 'Execute PAUSED Sandbox launch' })
        .click(),
    ]);
    const accepted =
      await readJson<LaunchProvisioningAccepted>(executeResponse);
    await expect(page).toHaveURL(
      new RegExp(`[?&]launchProvisioning=${accepted.id}(?:&|$)`)
    );

    // 9. Wait for provisioning, then verify every FakeMetaObject is PAUSED.
    const provisioned = (await waitForOperationState(
      page,
      WORKSPACE_SLUG,
      accepted.id,
      'customer_sandbox',
      'succeeded',
      60_000
    )) as LaunchProvisioningResponse;
    expect(provisioned.provisioning_state).toBe('provisioned');
    await expect(
      launchSection.getByText('provisioned', { exact: true })
    ).toBeVisible({
      timeout: 15_000,
    });

    const fakeObjects = inspectFakeMetaObjects(
      seed.workspaceId,
      'customer_sandbox',
      seed.customerSandboxScenarioVersionId
    );
    expect(fakeObjects.length).toBeGreaterThan(0);
    for (const object of fakeObjects) {
      expect((object.state as Record<string, unknown>).status).toBe('PAUSED');
    }

    // Browser refresh at this durable milestone: provisioning evidence must
    // survive purely from the preview/provisioning ids persisted in the URL.
    // The section is labelled by a paragraph, not a heading.
    await page.reload();
    await expect(
      page.getByText('Provisioning evidence', { exact: true })
    ).toBeVisible();

    // 10. Bind every candidate -- `start_experiment_v2` requires all three
    //     bound, independent of the Launch/provisioning path above.
    const afterLaunch = await readJson<ExperimentDetail>(
      await page.request.get(`${EXPERIMENTS_BFF}/${created.id}`)
    );
    const candidates = afterLaunch.candidates ?? [];
    expect(candidates).toHaveLength(3);
    const winnerCandidate = candidates.find(
      (candidate) =>
        candidate.asset_version_id === seed.duplicatePairAssetVersionIds[0]
    )!;
    const loserCandidate = candidates.find(
      (candidate) =>
        candidate.asset_version_id === seed.duplicatePairAssetVersionIds[1]
    )!;
    const independentCandidate = candidates.find(
      (candidate) =>
        candidate.asset_version_id === seed.independentAssetVersionId
    )!;
    const measureAccountId = `act_${seed.tag}_measure`;
    const winnerAdId = `ad_winner_${seed.tag}`;
    const loserAdId = `ad_loser_${seed.tag}`;

    await bindCandidateThroughUi(
      page,
      winnerCandidate.position,
      measureAccountId,
      winnerAdId
    );
    await bindCandidateThroughUi(
      page,
      loserCandidate.position,
      measureAccountId,
      loserAdId
    );
    await bindCandidateThroughUi(
      page,
      independentCandidate.position,
      measureAccountId,
      `ad_independent_${seed.tag}`
    );

    // 11. Start the experiment.
    const [startResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${EXPERIMENTS_BFF}/${created.id}/start`
      ),
      page.getByRole('button', { name: 'Start measurement' }).click(),
    ]);
    const running = await readJson<ExperimentDetail>(startResponse);
    expect(running.state).toBe('running');

    // 12. Seed decisive metrics (winner ~5% CTR, loser ~0.5% CTR against the
    //     frozen 1% threshold) and run judgement.
    const metrics = seedAdMetrics(seed, [
      {
        adAccountId: measureAccountId,
        adId: winnerAdId,
        dailyImpressions: 2000,
        dailyClicks: 100,
      },
      {
        adAccountId: measureAccountId,
        adId: loserAdId,
        dailyImpressions: 2000,
        dailyClicks: 10,
      },
    ]);
    expect(metrics.seeded_rows).toBe(14);

    const [judgeResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${EXPERIMENTS_BFF}/${created.id}/judgements`
      ),
      page.getByRole('button', { name: 'Run judgement' }).click(),
    ]);
    const judgement = await readJson<ExperimentJudgementResult>(judgeResponse);
    expect(judgement.proposal_state).toBe('created');

    // 13. The proposal must not already be a confirmed Winner.
    const proposalCard = page.locator('article').filter({
      has: page.getByRole('heading', { name: 'Judgement evidence' }),
    });
    await expect(proposalCard.getByText('Not confirmed')).toBeVisible();

    // 14. Acknowledge and confirm the Winner.
    await proposalCard
      .getByText(
        'I reviewed the evidence and understand this proposal does not become a Winner until I confirm it.'
      )
      .click();
    const [confirmProposalResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          /\/proposals\/.+\/confirm$/.test(new URL(response.url()).pathname)
      ),
      proposalCard.getByRole('button', { name: 'Confirm proposal' }).click(),
    ]);
    const confirmation = await readJson<ExperimentConfirmationResult>(
      confirmProposalResponse
    );
    expect(
      confirmation.decisions?.some(
        (decision) =>
          decision.verdict === 'win' || decision.verdict === 'winner'
      )
    ).toBe(true);

    // 15. The decision lives on the ExperimentDecisionRecord evidence
    //     surface, never on the AssetVersion itself.
    await expect(
      page.getByRole('heading', { name: 'Confirmed decisions' })
    ).toBeVisible();
    await expect(page.getByText('Confirmed Winner')).toBeVisible();

    const winnerAssetVersion = await readJson<AssetVersionDetail>(
      await page.request.get(
        `/api/domain/v1/w/${WORKSPACE_SLUG}/generation/asset-versions/${seed.duplicatePairAssetVersionIds[0]}`
      )
    );
    expect(winnerAssetVersion).not.toHaveProperty('decision');
    expect(winnerAssetVersion).not.toHaveProperty('decisions');
    expect(winnerAssetVersion).not.toHaveProperty('winner');

    // 16. Final durable-milestone refresh: the confirmed decision persists.
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'Confirmed decisions' })
    ).toBeVisible();
    await expect(page.getByText('Confirmed Winner')).toBeVisible();
  });

  test('Scenario 2a: stale revision detection resets the duplicate-resolution UI', async ({
    page,
  }) => {
    test.setTimeout(120_000);
    const seed = seedPrerequisites();
    const experimentName = `E2E stale revision ${seed.tag}`;

    // 1. Create an experiment carrying the seed's one duplicate pair.
    const created = await createDraftExperiment(page, seed, experimentName, [
      seed.duplicatePairAssetVersionIds[0],
      seed.duplicatePairAssetVersionIds[1],
    ]);
    expect(created.entries).toHaveLength(2);
    const staleRevision = created.revision;

    // 2. Open the analysis.
    await page.goto(`${EXPERIMENTS_URL}?experiment=${created.id}`);
    await expect(
      page.getByRole('heading', { name: experimentName })
    ).toBeVisible();
    await expect(
      page.getByRole('heading', { name: 'Duplicate Concept Resolution' })
    ).toBeVisible({ timeout: 30_000 });
    const analysis = await readJson<DuplicateAnalysisResponse>(
      await page.request.get(
        `${EXPERIMENTS_BFF}/${created.id}/duplicate-analysis`
      )
    );
    expect(analysis.has_duplicates).toBe(true);
    const group = analysis.duplicate_groups![0];

    await resolveDuplicateGroup(
      page,
      group,
      'allow_controlled_duplicate',
      'Allowed before this entry is mutated out from under the submission.'
    );
    const reasonInput = page.locator(`#reason-${group.duplicate_group_hash}`);
    await expect(reasonInput).not.toHaveValue('');

    await freezeAckCheckbox(page).check();
    await expect(freezeAckCheckbox(page)).toBeChecked();

    // 3. Mutate the entry server-side, out of band from this open tab --
    //    the tab's cached `experiment.revision`/analysis stay at the
    //    pre-mutation snapshot.
    const entryToMutate = created.entries![0];
    const mutateResponse = await page.request.put(
      `${EXPERIMENTS_BFF}/${created.id}/entries/${entryToMutate.id}`,
      {
        headers: await domainMutationHeaders(page),
        data: {
          marketing_vars: { mutated_out_of_band: true },
          expected_revision: staleRevision,
        },
      }
    );
    const mutated = await readJson<ExperimentEntryMutation>(mutateResponse);
    expect(mutated.revision).toBe(staleRevision + 1);

    // 4. Submit freeze with the tab's now-stale `expected_revision` ->
    //    the server rejects it as `revision_conflict`.
    const [freezeResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${EXPERIMENTS_BFF}/${created.id}/freeze`
      ),
      page.getByRole('button', { name: 'Freeze experiment' }).click(),
    ]);
    expect(freezeResponse.ok()).toBe(false);
    expect(freezeResponse.status()).toBe(409);
    const problem = (await freezeResponse.json()) as ProblemDetails;
    expect(problem.code).toBe('revision_conflict');

    // 5. The UI must reset: refetch, clear the resolution plan, and drop
    //    the freeze acknowledgement rather than allow the stale plan to be
    //    resubmitted. Next.js's route announcer is also `role="alert"`, so
    //    look inside the detail panel.
    await expect(
      page.getByRole('region', { name: 'Experiment detail' }).getByRole('alert')
    ).toBeVisible();
    await expect(freezeAckCheckbox(page)).not.toBeChecked();
    await expect(reasonInput).toHaveValue('');
    await expect(
      page.getByText(`revision ${staleRevision + 1}`, { exact: false })
    ).toBeVisible();
  });

  test('Scenario 8: no-duplicate freeze records explicit empty evidence', async ({
    page,
  }) => {
    test.setTimeout(60_000);
    const seed = seedPrerequisites();
    const experimentName = `E2E no duplicates ${seed.tag}`;

    // 1. Use only one member of the shared-concept pair plus the
    //    independent candidate: two distinct concept keys, zero duplicates.
    const created = await createDraftExperiment(page, seed, experimentName, [
      seed.duplicatePairAssetVersionIds[0],
      seed.independentAssetVersionId,
    ]);
    expect(created.entries).toHaveLength(2);

    await page.goto(`${EXPERIMENTS_URL}?experiment=${created.id}`);
    await expect(
      page.getByRole('heading', { name: experimentName })
    ).toBeVisible();

    // 2. Analysis must return `has_duplicates: false` and no groups, and the
    //    resolution panel must not render at all.
    const analysis = await readJson<DuplicateAnalysisResponse>(
      await page.request.get(
        `${EXPERIMENTS_BFF}/${created.id}/duplicate-analysis`
      )
    );
    expect(analysis.has_duplicates).toBe(false);
    expect(analysis.duplicate_groups ?? []).toHaveLength(0);
    await expect(
      page.getByRole('heading', { name: 'Duplicate Concept Resolution' })
    ).toHaveCount(0);

    // 3. Freeze with an empty plan and a valid analyzed-candidate-set hash.
    await freezeAckCheckbox(page).check();
    const [freezeRequest, freezeResponse] = await Promise.all([
      page.waitForRequest(
        (request) =>
          request.method() === 'POST' &&
          new URL(request.url()).pathname ===
            `${EXPERIMENTS_BFF}/${created.id}/freeze`
      ),
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${EXPERIMENTS_BFF}/${created.id}/freeze`
      ),
      page.getByRole('button', { name: 'Freeze experiment' }).click(),
    ]);
    expect(freezeRequest.postDataJSON().concept_resolution_plan).toEqual([]);

    const frozen = await readJson<ExperimentDetail>(freezeResponse);
    expect(frozen.state).toBe('frozen');

    // 4. `had_duplicates: false` evidence, both from the response and the
    //    rendered `FrozenResolutionEvidence` empty-state copy.
    expect(frozen.concept_resolution_evidence?.had_duplicates).toBe(false);
    expect(frozen.concept_resolution_evidence?.resolutions ?? []).toHaveLength(
      0
    );
    await expect(
      page.getByRole('heading', { name: 'Concept Resolution' })
    ).toBeVisible();
    await expect(
      page.getByText('No duplicate concepts detected at freeze time.')
    ).toBeVisible();
  });
});
