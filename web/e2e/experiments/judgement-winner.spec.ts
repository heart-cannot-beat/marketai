import { expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import {
  test,
  EXPERIMENTS_URL,
  EXPERIMENTS_BFF,
  seedPrerequisites,
  readJson,
  domainMutationHeaders,
  acknowledgeFreeze,
  expectFrozenPanel,
} from './fixtures';
import type { ExperimentsPrerequisites } from './fixtures';
import type { components } from '@/lib/domain-client/generated/domain-v1';

type ExperimentDetail = components['schemas']['ExperimentDetail'];
type ExperimentEntryMutation = components['schemas']['ExperimentEntryMutation'];
type ExperimentJudgementResult =
  components['schemas']['ExperimentJudgementResult'];

/** Shape of `JudgementRecord.result_json` (`app/experiments/judge.py::judge`).
 * The OpenAPI contract types `ExperimentJudgement.result` as a bare
 * `Record<string, unknown>`, so this narrows it for this spec's own
 * assertions -- it is not part of the generated contract. */
interface JudgeEntryResult {
  entry_id: string;
  verdict: string;
  metric: string;
  estimate: string | null;
  interval_low: string | null;
  interval_high: string | null;
  reason_codes: string[];
  totals: {
    days: number;
    impressions: number;
    clicks: number;
  };
}
interface JudgeResult {
  algorithm_version: string;
  entries: JudgeEntryResult[];
  summary: { win: number; learn: number; lose: number; inconclusive: number };
}

/**
 * Real-backend E2E coverage for issue #88's Scenario 3 (inconclusive
 * judgement). Consumes the Task 10 infrastructure -- `fixtures.ts`
 * (`seedPrerequisites`) -- and drives freeze / bind / start / judgement
 * through the real, rendered `ExperimentsWorkspace` UI against a live
 * PostgreSQL/FastAPI/Worker stack, exactly like `duplicate-resolution.spec.ts`
 * (Task 11) and `launch-recovery.spec.ts` (Task 12). Only pre-UI setup
 * (experiment + entry creation) goes through direct `page.request` calls.
 *
 * The golden-path *decisive* judgement/confirmation continuation from
 * Scenario 1 (win/lose verdicts, "Confirm proposal", "Confirmed Winner") is
 * already fully exercised by `duplicate-resolution.spec.ts`'s Scenario 1 test
 * (steps 12-16) -- this file covers only the inconclusive branch that
 * Scenario 1 never reaches (it seeds decisive metrics on purpose).
 *
 * This test needs a live Postgres/API/Worker stack; run it with
 * `npm run test:e2e:experiments` (`e2e/README.md`).
 */

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
 * seed's duplicate pair plus the independent candidate, which never share a
 * `DimensionConceptKey` -- so `analyze_duplicates` reports zero duplicate
 * groups. Duplicate resolution is orthogonal to judgement, so this uses the
 * simplest freeze path: an empty `concept_resolution_plan` (same combination
 * `launch-recovery.spec.ts` uses).
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

/** Opens the Experiment, freezes it with an empty (no-duplicate)
 * `concept_resolution_plan`, and returns the frozen `ExperimentDetail`. */
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

/** Binds one frozen candidate through the "Attach read-only Meta evidence"
 * card, using its rendered `candidate.position` (0-based) to find its "Ad
 * account for candidate N+1"/"Ad ID for candidate N+1" inputs and adjacent
 * "Bind" button. `bind_candidate_v2` only requires `state == "frozen"` --
 * it never validates the ad id/account against a real or mock Meta object,
 * so any string works here. */
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

test.describe('Judgement and Winner', () => {
  test('Scenario 3: inconclusive judgement — insufficient metric data produces no ExperimentDecisionRecord', async ({
    page,
  }) => {
    test.setTimeout(180_000);
    const seed = seedPrerequisites();
    const experimentName = `E2E inconclusive judgement ${seed.tag}`;

    // 1. Create, freeze, bind, and start through the golden path -- but
    //    never seed any `AdDailyMetricRecord` rows for the bound ad ids.
    //    `run_judgement_v2`'s snapshot only ever sees metrics through
    //    `(AdDailyMetricRecord.ad_id, metric_date <= cutoff)`
    //    (`app/experiments/service.py`), so with zero rows every candidate's
    //    `daily` list is empty.
    const created = await createTwoCandidateExperiment(
      page,
      seed,
      experimentName
    );
    expect(created.entries).toHaveLength(2);
    const frozen = await openAndFreeze(page, created.id, experimentName);

    const candidates = frozen.candidates ?? [];
    expect(candidates).toHaveLength(2);
    for (let index = 0; index < candidates.length; index++) {
      const candidate = candidates[index];
      await bindCandidateThroughUi(
        page,
        candidate.position,
        `act_${seed.tag}_inconclusive`,
        `ad_inconclusive_${seed.tag}_${index}`
      );
    }

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

    // 2. Run judgement with zero metric evidence -- `judge()`'s CTR branch
    //    (`app/experiments/judge.py`) appends `insufficient_days` (via
    //    `len(daily) < config.get("min_days", 1)`, which defaults to `1`)
    //    for every entry and returns `verdict: "inconclusive"` for each.
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
    // Eligibility (`is_effectively_eligible`) defaults to `true` with no
    // eligibility events recorded, so the proposal is still `created` --
    // "inconclusive" is a property of every proposed entry's verdict, not of
    // proposal creation itself.
    expect(judgement.proposal_state).toBe('created');
    expect(judgement.proposal_id).toBeTruthy();

    expect(judgement.judgement.result).toBeDefined();
    const result = judgement.judgement.result as unknown as JudgeResult;
    expect(result.summary.inconclusive).toBe(2);
    expect(result.summary.win).toBe(0);
    expect(result.summary.lose).toBe(0);
    expect(result.summary.learn).toBe(0);
    for (const entry of result.entries) {
      expect(entry.verdict).toBe('inconclusive');
      expect(entry.reason_codes).toContain('insufficient_days');
      expect(entry.totals.days).toBe(0);
      expect(entry.estimate).toBeNull();
    }

    // 3. No `ExperimentDecisionRecord` was ever created --
    //    `confirm_proposal_v2` is the only place that inserts one, and the
    //    UI's `ProposalCard` disables confirmation entirely once every
    //    proposed entry is inconclusive (`inconclusive =
    //    decisiveEntries.length === 0` in `ExperimentsWorkspace.tsx`), so
    //    this test never even attempts to click a "Confirm proposal" button.
    const proposalCard = page.locator('article').filter({
      has: page.getByRole('heading', { name: 'Judgement evidence' }),
    });
    await expect(proposalCard.getByText('Not confirmed')).toBeVisible();
    await expect(
      proposalCard.getByText(
        'Inconclusive evidence cannot be confirmed as a Winner and unlocks no downstream action.'
      )
    ).toBeVisible();
    await expect(
      proposalCard.getByRole('button', { name: 'No Winner to confirm' })
    ).toBeDisabled();
    await expect(
      page.getByRole('heading', { name: 'Confirmed decisions' })
    ).toHaveCount(0);

    const afterJudgement = await readJson<ExperimentDetail>(
      await page.request.get(`${EXPERIMENTS_BFF}/${created.id}`)
    );
    expect(afterJudgement.decisions ?? []).toHaveLength(0);

    // 4. Refresh -- the inconclusive state (and the absence of any decision)
    //    persists purely from durable server state, not browser memory.
    await page.reload();
    await expect(
      page.getByRole('heading', { name: experimentName })
    ).toBeVisible();
    const proposalCardAfterReload = page.locator('article').filter({
      has: page.getByRole('heading', { name: 'Judgement evidence' }),
    });
    await expect(
      proposalCardAfterReload.getByText('Not confirmed')
    ).toBeVisible();
    await expect(
      proposalCardAfterReload.getByRole('button', {
        name: 'No Winner to confirm',
      })
    ).toBeDisabled();
    await expect(
      page.getByRole('heading', { name: 'Confirmed decisions' })
    ).toHaveCount(0);

    const afterReload = await readJson<ExperimentDetail>(
      await page.request.get(`${EXPERIMENTS_BFF}/${created.id}`)
    );
    expect(afterReload.decisions ?? []).toHaveLength(0);
    expect(afterReload.state).toBe('running');
  });
});
