import { expect } from '@playwright/test';
import type {
  BrandedOpaqueId,
  EntityKind,
  EntityRef,
  ExperimentClaimCode,
  ExperimentFrozenEvidence,
  ExperimentInconclusiveEvidence,
  ExperimentLaunchedEvidence,
  ExperimentWinnerEvidence,
} from '../../artifacts/schemas';
import type { OwnerSessionCapability } from '../onboarding-harness';

function entityRef<K extends EntityKind>(
  kind: K,
  runtimeId: string,
  observedVia: 'dom' | 'same_origin_response'
): EntityRef<K> {
  return {
    kind,
    runtimeId: runtimeId as BrandedOpaqueId<K>,
    observedVia,
  };
}

export class ExperimentsDriver {
  private workspaceSlug: string;

  constructor(workspaceSlug: string) {
    this.workspaceSlug = workspaceSlug;
  }

  setWorkspaceSlug(workspaceSlug: string): void {
    this.workspaceSlug = workspaceSlug;
  }

  async createAndFreeze(
    session: OwnerSessionCapability,
    opts: {
      approvedAssets: EntityRef<'asset_version'>[];
      reApprovedAsset: EntityRef<'asset_version'>;
      productVersion: EntityRef<'product_version'>;
      offerVersion: EntityRef<'offer_version'>;
    }
  ): Promise<ExperimentFrozenEvidence> {
    const page = session.page();
    const base = `/w/${this.workspaceSlug}`;

    await page.goto(`${base}/legacy-experiments`);
    const allCandidates = [...opts.approvedAssets, opts.reApprovedAsset];
    await expect(
      page.getByRole('heading', { name: 'Experiments', exact: true })
    ).toBeVisible();
    await page.getByRole('button', { name: /New experiment/i }).click();
    const createPanel = page.locator('[aria-label="Create experiment"]');
    await expect(createPanel).toBeVisible();
    await createPanel
      .getByPlaceholder(/creative test/i)
      .fill('Golden Path Experiment');

    for (const candidate of allCandidates) {
      const row = createPanel
        .locator('label')
        .filter({ hasText: candidate.runtimeId })
        .first();
      await expect(row).toBeVisible();
      await row.locator('input[type="checkbox"]').check();
    }

    await createPanel
      .getByLabel(/Product version ID/i)
      .fill(opts.productVersion.runtimeId);
    await createPanel
      .getByLabel(/Offer version ID/i)
      .fill(opts.offerVersion.runtimeId);
    await createPanel.getByLabel(/^Market$/i).fill('US');
    await createPanel.getByLabel(/^Currency$/i).fill('USD');
    await createPanel.getByLabel(/Placement/i).selectOption('feed');
    await createPanel.getByLabel(/Daily budget/i).fill('50.00');
    await createPanel.getByLabel(/Total budget cap/i).fill('400.00');
    const startDate = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000)
      .toISOString()
      .slice(0, 10);
    const endDate = new Date(Date.now() + 14 * 24 * 60 * 60 * 1000)
      .toISOString()
      .slice(0, 10);
    await createPanel.getByLabel(/Start date/i).fill(startDate);
    await createPanel.getByLabel(/End date/i).fill(endDate);

    const [createResponse] = await Promise.all([
      page.waitForResponse(
        (res) =>
          res.url().includes('/experiments') &&
          res.request().method() === 'POST' &&
          res.status() === 201
      ),
      createPanel.getByRole('button', { name: /Create draft/i }).click(),
    ]);

    const createBody = (await createResponse.json()) as { id?: string };
    const experimentId = createBody.id;
    if (!experimentId) throw new Error('Experiment response returned no id');
    await page.waitForURL(
      (url) => url.searchParams.get('experiment') === experimentId,
      { timeout: 15_000 }
    );
    await expect
      .poll(
        async () => {
          const response = await page.request.get(
            `/api/domain/v1/w/${this.workspaceSlug}/experiments/${experimentId}`
          );
          if (!response.ok()) return 0;
          const body = (await response.json()) as { entries?: unknown[] };
          return body.entries?.length ?? 0;
        },
        { timeout: 15_000 }
      )
      .toBe(allCandidates.length);

    await page.goto(`${base}/legacy-experiments?experiment=${experimentId}`);
    await expect(
      page.getByRole('heading', { name: 'Golden Path Experiment', exact: true })
    ).toBeVisible();
    const duplicateAnalysisResponse = await page.request.get(
      `/api/domain/v1/w/${this.workspaceSlug}/experiments/${experimentId}/duplicate-analysis`
    );
    if (!duplicateAnalysisResponse.ok()) {
      throw new Error(
        `Duplicate analysis request failed (${duplicateAnalysisResponse.status()})`
      );
    }
    const duplicateAnalysis = (await duplicateAnalysisResponse.json()) as {
      has_duplicates?: boolean;
      duplicate_groups?: unknown[];
    };
    if (duplicateAnalysis.has_duplicates) {
      const groupCount = duplicateAnalysis.duplicate_groups?.length ?? 0;
      await expect(
        page.getByRole('heading', { name: 'Duplicate Concept Resolution' })
      ).toBeVisible({ timeout: 15_000 });
      const allowDuplicate = page.getByLabel('Allow (controlled duplicate)');
      const reasons = page.getByPlaceholder(
        'Explain this resolution decision…'
      );
      await expect(allowDuplicate).toHaveCount(groupCount);
      await expect(reasons).toHaveCount(groupCount);
      for (let index = 0; index < groupCount; index += 1) {
        await allowDuplicate.nth(index).check();
        await reasons
          .nth(index)
          .fill('Golden path controlled-duplicate coverage.');
      }
    }
    const acknowledgement = page.getByLabel(
      /I reviewed the candidates, budget, schedule, and immutable context/i
    );
    const freezeButton = page.getByRole('button', {
      name: /Freeze experiment/i,
    });
    // The page clears this acknowledgement once, when its own
    // duplicate-analysis result first arrives (a stale plan must never be
    // frozen). The request above is the driver's, not the page's, so the
    // tick can race that reset. Keep acknowledgement and the actual click in
    // one retry: checking enabled separately leaves another reset window.
    const freezeResponsePromise = page.waitForResponse(
      (response) =>
        response.url().includes(`/experiments/${experimentId}/freeze`) &&
        response.request().method() === 'POST' &&
        (response.status() === 200 || response.status() === 201),
      { timeout: 45_000 }
    );
    const [freezeResponse] = await Promise.all([
      freezeResponsePromise,
      expect(async () => {
        if (!(await acknowledgement.isChecked())) await acknowledgement.check();
        await expect(freezeButton).toBeEnabled({ timeout: 1_000 });
        await freezeButton.click({ timeout: 1_000 });
      }).toPass({ timeout: 30_000, intervals: [500, 1_000, 2_000] }),
    ]);
    const frozenBody = (await freezeResponse.json()) as {
      state?: string;
    };
    if (frozenBody.state !== 'frozen') {
      throw new Error(
        `Experiment freeze returned ${frozenBody.state ?? 'unknown'}`
      );
    }

    const candidateRefs = allCandidates.map((c) =>
      entityRef('asset_version', c.runtimeId, 'dom')
    );

    return {
      schemaVersion: 1,
      area: 'experiments',
      claims: [
        {
          code: 'experiment_frozen' as ExperimentClaimCode,
          status: 'passed',
        },
      ],
      experiment: entityRef('experiment', experimentId, 'same_origin_response'),
      candidateRefs,
    };
  }

  async launchPaused(
    session: OwnerSessionCapability,
    opts: {
      experiment: EntityRef<'experiment'>;
    }
  ): Promise<ExperimentLaunchedEvidence> {
    const page = session.page();
    const base = `/w/${this.workspaceSlug}`;
    await page.goto(
      `${base}/legacy-experiments?experiment=${opts.experiment.runtimeId}`
    );
    await expect(
      page.getByRole('heading', { name: 'Golden Path Experiment', exact: true })
    ).toBeVisible();

    const detailResponse = await page.request.get(
      `/api/domain/v1/w/${this.workspaceSlug}/experiments/${opts.experiment.runtimeId}`
    );
    if (!detailResponse.ok()) {
      throw new Error(
        `Experiment detail request failed (${detailResponse.status()})`
      );
    }
    const detail = (await detailResponse.json()) as {
      candidates?: Array<{
        id: string;
        asset_version_id: string;
        binding?: unknown;
      }>;
    };
    const candidates = detail.candidates ?? [];
    if (candidates.length < 2)
      throw new Error('Frozen experiment has no candidates');

    // Frozen candidates must be bound before Start measurement becomes enabled.
    for (let index = 0; index < candidates.length; index += 1) {
      const candidate = candidates[index];
      const card = page.getByRole('group', { name: `Candidate ${index + 1}` });
      if (candidate.binding) continue;
      await card
        .getByLabel(`Ad account for candidate ${index + 1}`)
        .fill(`act_gp_exp_${opts.experiment.runtimeId.slice(0, 8)}`);
      await card
        .getByLabel(`Ad ID for candidate ${index + 1}`)
        .fill(`ad_${candidate.id.slice(0, 16)}`);
      const bindResponse = page.waitForResponse(
        (response) =>
          response.url().includes(`/candidates/${candidate.id}/bind`) &&
          response.request().method() === 'POST' &&
          response.status() === 200
      );
      await card.getByRole('button', { name: /^Bind$/i }).click();
      await bindResponse;
      await expect(card.getByText('Bound', { exact: true })).toBeVisible();
    }

    const connectionResponse = await page.request.get(
      `/api/domain/v1/w/${this.workspaceSlug}/meta/connections?meta_mode=customer_sandbox`
    );
    if (!connectionResponse.ok()) {
      throw new Error(
        `Meta connection request failed (${connectionResponse.status()})`
      );
    }
    const connectionBody = (await connectionResponse.json()) as {
      items?: Array<{ id: string; ad_account_id: string; state: string }>;
    };
    const connection = connectionBody.items?.find(
      (item) => item.state === 'active'
    );
    if (!connection)
      throw new Error('No active customer_sandbox Meta connection');

    await page.getByLabel(/Sandbox Meta connection ID/i).fill(connection.id);
    await page
      .getByLabel(/Sandbox Meta ad account ID/i)
      .fill(connection.ad_account_id);
    await page.getByLabel(/Daily budget per candidate/i).fill('500');

    const previewResponsePromise = page.waitForResponse(
      (response) =>
        response.url().includes('/launch/previews') &&
        response.request().method() === 'POST'
    );
    await page.getByRole('button', { name: /Create Sandbox preview/i }).click();
    const previewResponse = await previewResponsePromise;
    if (!previewResponse.ok()) {
      throw new Error(
        `Sandbox preview failed (${previewResponse.status()}): ${await previewResponse.text()}`
      );
    }
    const preview = (await previewResponse.json()) as {
      id: string;
      preview_hash: string;
    };

    await page.getByLabel(/I reviewed every exact candidate/i).check();
    const confirmResponsePromise = page.waitForResponse(
      (response) =>
        response.url().includes(`/launch/previews/${preview.id}/confirm`) &&
        response.request().method() === 'POST'
    );
    await page
      .getByRole('button', { name: /Confirm Sandbox preview/i })
      .click();
    const confirmResponse = await confirmResponsePromise;
    if (!confirmResponse.ok()) {
      throw new Error(
        `Sandbox preview confirmation failed (${confirmResponse.status()}): ${await confirmResponse.text()}`
      );
    }

    await page.getByLabel(/Execute this exact/i).check();
    const executeResponsePromise = page.waitForResponse(
      (response) =>
        response.url().includes(`/launch/previews/${preview.id}/execute`) &&
        response.request().method() === 'POST'
    );
    await page
      .getByRole('button', { name: /Execute PAUSED Sandbox launch/i })
      .click();
    const executeResponse = await executeResponsePromise;
    if (!executeResponse.ok()) {
      throw new Error(
        `Sandbox launch execution failed (${executeResponse.status()}): ${await executeResponse.text()}`
      );
    }
    const executed = (await executeResponse.json()) as {
      id: string;
      operation_id: string;
    };

    await expect
      .poll(
        async () => {
          const response = await page.request.get(
            `/api/domain/v1/w/${this.workspaceSlug}/launch/operations/${executed.id}?meta_mode=customer_sandbox`
          );
          if (!response.ok()) return 'missing';
          const body = (await response.json()) as {
            provisioning_state?: string;
          };
          return body.provisioning_state ?? 'missing';
        },
        { timeout: 60_000, intervals: [2_000] }
      )
      .toBe('provisioned');
    await page.reload();
    await expect(page.locator('[data-provisioning-state]')).toHaveText(
      'provisioned'
    );

    const startResponse = page.waitForResponse(
      (response) =>
        response
          .url()
          .includes(`/experiments/${opts.experiment.runtimeId}/start`) &&
        response.request().method() === 'POST' &&
        (response.status() === 200 || response.status() === 201)
    );
    await page.getByRole('button', { name: /Start measurement/i }).click();
    const started = (await (await startResponse).json()) as { state?: string };
    if (started.state !== 'running') {
      throw new Error(
        `Experiment start returned ${started.state ?? 'unknown'}`
      );
    }

    const provisioningResponse = await page.request.get(
      `/api/domain/v1/w/${this.workspaceSlug}/launch/operations/${executed.id}?meta_mode=customer_sandbox`
    );
    const provisioning = (await provisioningResponse.json()) as {
      steps?: Array<{ step_type: string; remote_object_id?: string | null }>;
    };
    const stepIds = (stepType: string) =>
      (provisioning.steps ?? [])
        .filter((step) => step.step_type === stepType && step.remote_object_id)
        .map((step) => step.remote_object_id as string);
    const campaignId = stepIds('create_campaign')[0];
    const adSetIds = stepIds('create_adset');
    const adIds = stepIds('create_ad');
    if (
      !campaignId ||
      adSetIds.length < candidates.length ||
      adIds.length < candidates.length
    ) {
      throw new Error(
        'Launch provisioning returned incomplete Meta object refs'
      );
    }
    const candidateDeliveries = candidates.map((candidate, index) => ({
      candidateRef: entityRef(
        'asset_version',
        candidate.asset_version_id,
        'same_origin_response'
      ),
      adSetRef: entityRef('ad_set', adSetIds[index], 'same_origin_response'),
      adRef: entityRef('ad', adIds[index], 'same_origin_response'),
    }));

    return {
      schemaVersion: 1,
      area: 'experiments',
      claims: [
        {
          code: 'experiment_launched' as ExperimentClaimCode,
          status: 'passed',
        },
      ],
      experiment: opts.experiment,
      campaignRef: entityRef('campaign', campaignId, 'same_origin_response'),
      adSetRefs: adSetIds.map((id) =>
        entityRef('ad_set', id, 'same_origin_response')
      ),
      adRefs: adIds.map((id) => entityRef('ad', id, 'same_origin_response')),
      candidateDeliveries,
    };
  }

  async recordInconclusive(
    session: OwnerSessionCapability,
    opts: {
      experiment: EntityRef<'experiment'>;
    }
  ): Promise<ExperimentInconclusiveEvidence> {
    const page = session.page();
    const base = `/w/${this.workspaceSlug}`;
    await page.goto(
      `${base}/legacy-experiments?experiment=${opts.experiment.runtimeId}`
    );
    await expect(
      page.getByRole('heading', { name: 'Golden Path Experiment', exact: true })
    ).toBeVisible();
    const judgeResponse = page.waitForResponse(
      (response) =>
        response
          .url()
          .includes(`/experiments/${opts.experiment.runtimeId}/judgements`) &&
        response.request().method() === 'POST' &&
        (response.status() === 200 || response.status() === 201)
    );
    await page.getByRole('button', { name: /Run judgement/i }).click();
    const judged = (await (await judgeResponse).json()) as {
      judgement?: {
        result?: {
          entries?: Array<{ verdict?: string }>;
          summary?: Record<string, number>;
        };
      };
      proposal_id?: string | null;
    };
    const result = judged.judgement?.result;
    expect(result?.summary?.inconclusive).toBeGreaterThan(0);
    expect(
      result?.entries?.every((entry) => entry.verdict === 'inconclusive')
    ).toBe(true);
    if (!judged.proposal_id)
      throw new Error('Inconclusive judgement returned no proposal');
    const proposal = page.locator(`[data-proposal-id="${judged.proposal_id}"]`);
    await expect(proposal.getByRole('status')).toContainText(
      /Inconclusive evidence cannot be confirmed/i
    );
    await expect(
      proposal.getByRole('button', { name: 'No Winner to confirm' })
    ).toBeDisabled();

    return {
      schemaVersion: 1,
      area: 'experiments',
      claims: [
        {
          code: 'experiment_inconclusive' as ExperimentClaimCode,
          status: 'passed',
        },
      ],
      experiment: opts.experiment,
    };
  }

  async confirmWinner(
    session: OwnerSessionCapability,
    opts: {
      experiment: EntityRef<'experiment'>;
    }
  ): Promise<ExperimentWinnerEvidence> {
    const page = session.page();
    const base = `/w/${this.workspaceSlug}`;
    await page.goto(
      `${base}/legacy-experiments?experiment=${opts.experiment.runtimeId}`
    );
    await expect(
      page.getByRole('heading', { name: 'Golden Path Experiment', exact: true })
    ).toBeVisible();
    const judgeResponse = page.waitForResponse(
      (response) =>
        response
          .url()
          .includes(`/experiments/${opts.experiment.runtimeId}/judgements`) &&
        response.request().method() === 'POST' &&
        (response.status() === 200 || response.status() === 201)
    );
    await page.getByRole('button', { name: /Run judgement/i }).click();
    const judged = (await (await judgeResponse).json()) as {
      proposal_id?: string | null;
    };
    if (!judged.proposal_id)
      throw new Error('Decisive judgement returned no proposal');
    const proposal = page.locator(`[data-proposal-id="${judged.proposal_id}"]`);
    await expect(
      proposal.getByRole('button', { name: 'Confirm proposal' })
    ).toBeVisible();
    await proposal
      .getByLabel(
        /I reviewed the evidence and understand this proposal does not become a Winner until I confirm it/i
      )
      .check();
    const confirmResponse = page.waitForResponse(
      (response) =>
        response
          .url()
          .includes(
            `/experiments/${opts.experiment.runtimeId}/proposals/${judged.proposal_id}/confirm`
          ) &&
        response.request().method() === 'POST' &&
        (response.status() === 200 || response.status() === 201)
    );
    await proposal.getByRole('button', { name: 'Confirm proposal' }).click();
    const confirmed = (await (await confirmResponse).json()) as {
      decisions?: Array<{
        id: string;
        verdict: string;
        candidate_id?: string | null;
      }>;
    };
    const winnerDecision = confirmed.decisions?.find(
      (decision) => decision.verdict === 'win' || decision.verdict === 'winner'
    );
    if (!winnerDecision?.id || !winnerDecision.candidate_id) {
      throw new Error('Winner confirmation returned no winning decision');
    }
    const detailResponse = await page.request.get(
      `/api/domain/v1/w/${this.workspaceSlug}/experiments/${opts.experiment.runtimeId}`
    );
    const detail = (await detailResponse.json()) as {
      candidates?: Array<{ id: string; asset_version_id: string }>;
    };
    const winnerCandidate = detail.candidates?.find(
      (candidate) => candidate.id === winnerDecision.candidate_id
    );
    if (!winnerCandidate)
      throw new Error('Winning candidate is missing from experiment detail');

    return {
      schemaVersion: 1,
      area: 'experiments',
      claims: [
        {
          code: 'experiment_winner_confirmed' as ExperimentClaimCode,
          status: 'passed',
        },
      ],
      experiment: opts.experiment,
      winnerAssetVersion: entityRef(
        'asset_version',
        winnerCandidate.asset_version_id,
        'same_origin_response'
      ),
      winnerDecision: entityRef(
        'winner_decision',
        winnerDecision.id,
        'same_origin_response'
      ),
    };
  }

  async verifyPersistedState(
    session: OwnerSessionCapability,
    evidence: ExperimentWinnerEvidence
  ): Promise<void> {
    const page = session.page();
    const base = `/w/${this.workspaceSlug}`;

    await page.goto(
      `${base}/legacy-experiments?experiment=${evidence.experiment.runtimeId}`
    );
    await expect(
      page.getByRole('heading', { name: 'Golden Path Experiment', exact: true })
    ).toBeVisible();
    const response = await page.request.get(
      `/api/domain/v1/w/${this.workspaceSlug}/experiments/${evidence.experiment.runtimeId}`
    );
    expect(response.ok()).toBe(true);
    const body = (await response.json()) as {
      state?: string;
      candidates?: Array<{ asset_version_id: string }>;
      decisions?: Array<{
        id: string;
        candidate_id?: string | null;
        verdict: string;
      }>;
    };
    // Winner confirmation is the final reachable experiment milestone in the
    // Golden Path. The current v1/UI surface has no close command, so the
    // durable state remains running; closed-state evidence is covered by the
    // dedicated hybrid fixture in the experiments suite.
    expect(body.state).toBe('running');
    expect(
      body.decisions?.some(
        (decision) => decision.id === evidence.winnerDecision.runtimeId
      )
    ).toBe(true);
    const winningDecision = body.decisions?.find(
      (decision) => decision.id === evidence.winnerDecision.runtimeId
    );
    const winnerCandidate = body.candidates?.find(
      (candidate) =>
        candidate.asset_version_id === evidence.winnerAssetVersion.runtimeId
    );
    expect(
      winningDecision?.verdict === 'win' ||
        winningDecision?.verdict === 'winner'
    ).toBe(true);
    expect(winnerCandidate).toBeDefined();
  }
}
