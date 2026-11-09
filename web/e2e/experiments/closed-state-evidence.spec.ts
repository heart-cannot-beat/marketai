import { execFileSync } from 'child_process';
import path from 'path';
import { expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import {
  test,
  EXPERIMENTS_URL,
  EXPERIMENTS_BFF,
  seedPrerequisites,
  readJson,
  domainMutationHeaders,
} from './fixtures';
import type { ExperimentsPrerequisites } from './fixtures';
import type { components } from '@/lib/domain-client/generated/domain-v1';

type ExperimentDetail = components['schemas']['ExperimentDetail'];
type ExperimentEntryMutation = components['schemas']['ExperimentEntryMutation'];
type DuplicateAnalysisResponse =
  components['schemas']['DuplicateAnalysisResponse'];
type DuplicateGroup = components['schemas']['DuplicateGroup'];

/**
 * Real-backend E2E coverage for issue #88's spec §3.5 (closed-state
 * evidence). Consumes the Task 10 infrastructure (`fixtures.ts`'s
 * `seedPrerequisites`) and drives the real freeze/resolution path through
 * the rendered `ExperimentsWorkspace` UI, exactly like
 * `duplicate-resolution.spec.ts` (Task 11) -- except for the one, and only,
 * manufactured step §3.5 explicitly allows: flipping `experiment.state` to
 * `"closed"` via a direct, test-only database update
 * (`set_experiment_closed.py`, co-located with this spec).
 *
 * `close_experiment_v2` (`app/experiments/service.py`) exists but is never
 * wired to an HTTP route, and `ExperimentsWorkspace.tsx` has no "Close
 * experiment" action -- there is no reachable way to observe closed-state
 * evidence display through the real product surface today. Per §3.5's
 * hybrid-fixture rule, every other piece of evidence (the frozen
 * `ExperimentVersionRecord`, its candidates, and -- the one this test cares
 * about -- `ConceptResolutionEvidence`/`concept_resolutions_hash`) comes
 * from the real `freeze_experiment_v2` service, driven through the real UI
 * exactly like `duplicate-resolution.spec.ts`'s Scenario 1. Only the state
 * transition itself is manufactured.
 *
 * This test needs a live Postgres/API/Worker stack; run it with
 * `npm run test:e2e:experiments` (`e2e/README.md`).
 */

const FREEZE_ACK_TEXT =
  'I reviewed the candidates, budget, schedule, and immutable context.';

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

/** A draft Experiment carrying the seed's one real duplicate pair plus the
 * independent candidate -- the same combination
 * `duplicate-resolution.spec.ts`'s Scenario 1 uses, so freezing this
 * produces genuine, non-empty `ConceptResolutionEvidence` (not the trivial
 * `had_duplicates: false` case). */
async function createDraftExperiment(
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
    seed.duplicatePairAssetVersionIds[1],
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

function freezeAckCheckbox(page: Page) {
  return page
    .locator('label')
    .filter({ hasText: FREEZE_ACK_TEXT })
    .locator('input[type="checkbox"]');
}

/**
 * Resolves one `DuplicateResolutionPanel` group card -- the same helper
 * `duplicate-resolution.spec.ts` uses.
 */
async function resolveDuplicateGroup(
  page: Page,
  group: DuplicateGroup,
  reason: string
): Promise<void> {
  const hash = group.duplicate_group_hash;
  await page
    .locator(`input[name="action-${hash}"][value="allow_controlled_duplicate"]`)
    .check();
  await page.locator(`#reason-${hash}`).fill(reason);
}

/**
 * Directly flips `experiment.state` to `"closed"` via
 * `set_experiment_closed.py`, mirroring `seedPrerequisites`'s/
 * `seed_ad_metrics.py`'s own `execFileSync` shell-out pattern (same
 * mutation-safety preflight: opt-in env var, PostgreSQL/owner-role/
 * local-host checks). This never bypasses the RLS `tenant_session`
 * boundary -- it only manufactures the one state transition §3.5 says no
 * reachable command can currently produce.
 */
function closeExperimentDirectly(
  workspaceId: string,
  experimentId: string
): {
  experiment_id: string;
  from_state: string;
  state: string;
  closed_at: string;
} {
  const repositoryRoot = path.resolve(__dirname, '..', '..', '..');
  const python = process.env.E2E_PYTHON ?? 'python3';
  const script = path.join(__dirname, 'set_experiment_closed.py');
  const raw = execFileSync(
    python,
    [script, '--workspace-id', workspaceId, '--experiment-id', experimentId],
    {
      cwd: repositoryRoot,
      env: { ...process.env, PYTHONPATH: repositoryRoot },
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'inherit'],
    }
  );
  return JSON.parse(raw.trim()) as {
    experiment_id: string;
    from_state: string;
    state: string;
    closed_at: string;
  };
}

test.describe('Closed-state evidence display', () => {
  test('§3.5: evidence produced by a real freeze, only the closed state is manufactured', async ({
    page,
  }) => {
    test.setTimeout(120_000);
    const seed = seedPrerequisites();
    const experimentName = `E2E closed-state evidence ${seed.tag}`;

    // 1. Produce evidence through production behavior: create, resolve the
    //    real duplicate group, and freeze through the real
    //    `freeze_experiment_v2` service, driven through the rendered UI --
    //    identical to `duplicate-resolution.spec.ts`'s Scenario 1 steps 1-6.
    const created = await createDraftExperiment(page, seed, experimentName);
    expect(created.entries).toHaveLength(3);

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
    expect(analysis.duplicate_groups).toHaveLength(1);
    const group = analysis.duplicate_groups![0];

    const freezeReason =
      'Both creative variants stay in market; duplicate concept is intentional for this test.';
    await resolveDuplicateGroup(page, group, freezeReason);
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
    const frozenEvidence = frozen.concept_resolution_evidence!;

    // Sanity: the frozen evidence renders through the real UI before this
    // test manufactures anything, establishing the baseline §3.5 asks the
    // closed-state read to match.
    await expect(
      page.getByRole('heading', { name: 'Concept Resolution' })
    ).toBeVisible();
    await expect(page.getByText(freezeReason, { exact: false })).toBeVisible();

    // 2. Transition only the unreachable state: a direct, test-only
    //    database update -- the one manufactured outcome, and only the
    //    state transition itself.
    const closeResult = closeExperimentDirectly(seed.workspaceId, created.id);
    expect(closeResult.from_state).toBe('frozen');
    expect(closeResult.state).toBe('closed');

    // 3. Navigate to the experiment (a fresh load, not a same-tab refresh,
    //    to prove this reads the manufactured server state rather than
    //    replaying cached frozen-state browser memory).
    await page.goto(`${EXPERIMENTS_URL}?experiment=${created.id}`);
    await expect(
      page.getByRole('heading', { name: experimentName })
    ).toBeVisible();

    const closedRead = await readJson<ExperimentDetail>(
      await page.request.get(`${EXPERIMENTS_BFF}/${created.id}`)
    );
    expect(closedRead.state).toBe('closed');

    // 4. Verify the evidence section renders identically to the frozen
    //    state, using the same `FrozenResolutionEvidence` component --
    //    `ExperimentsWorkspace.tsx` renders it whenever
    //    `experiment.concept_resolution_evidence` is present, unconditional
    //    on `experiment.state` (unlike the "Next action" sidebar, which
    //    does branch on state -- see the closed-only assertion below).
    await expect(
      page.getByRole('heading', { name: 'Concept Resolution' })
    ).toBeVisible();
    await expect(page.getByText(freezeReason, { exact: false })).toBeVisible();
    await expect(page.getByText('Allowed duplicate')).toBeVisible();

    // 5. Compare evidence hash, resolutions, and display byte-for-byte
    //    against the pre-close frozen read -- the manufactured state flip
    //    must not have touched the evidence payload at all.
    const closedEvidence = closedRead.concept_resolution_evidence!;
    expect(closedEvidence.concept_resolutions_hash).toBe(
      frozenEvidence.concept_resolutions_hash
    );
    expect(closedEvidence.had_duplicates).toBe(frozenEvidence.had_duplicates);
    expect(closedEvidence.resolutions).toEqual(frozenEvidence.resolutions);

    // The one thing that *does* differ is the state-dependent "Next action"
    // sidebar -- proving this test actually observed the closed branch
    // (`experiment.state === 'closed'` in `ExperimentsWorkspace.tsx`), not a
    // stale frozen-state render.
    await expect(
      page.getByText('Experiment closed with human-confirmed decisions.')
    ).toBeVisible();
    await expect(
      page.getByRole('button', { name: 'Freeze experiment' })
    ).toHaveCount(0);
  });
});
