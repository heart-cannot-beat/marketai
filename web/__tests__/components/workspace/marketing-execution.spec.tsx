/** @jest-environment jsdom */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { SWRConfig } from 'swr';
import { MarketingExecution } from '@/components/workspace/MarketingExecution';
import type { MarketingRound } from '@/components/workspace/MarketingExecution';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { boardExecutions } from '@/components/workspace/marketing-focus';
import fixture from '../../fixtures/marketing-workflow.json';
import qcFixture from '../../fixtures/marketing-ai-qc.json';
import { roundRead, storedPlan } from '../../helpers/marketing-round-read';
import messages from '../../../locales/en/common.json';

jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      ((messages as Record<string, string>)[key] ?? key).replace(
        /\{\{(\w+)\}\}/g,
        (_: string, name: string) =>
          String(
            options?.[name] ??
              // `errorCodeMessage` passes a code's params as i18next `replace`.
              (options?.replace as Record<string, unknown> | undefined)?.[
                name
              ] ??
              ''
          )
      ),
    i18n: { language: 'en' },
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws', slug: 'alpha' } }),
}));
const gateway = {
  getMarketingWorkflow: jest.fn(),
  getMarketingAIQCQuote: jest.fn(),
  startMarketingAIQC: jest.fn(),
  confirmMarketingAIQC: jest.fn(),
  createGenerationAssetVersionDownloadIntent: jest.fn(),
  freezeMarketingRound: jest.fn(),
  reviewMarketingCreative: jest.fn(),
  previewMarketingMetrics: jest.fn(),
  confirmMarketingMetrics: jest.fn(),
  closeMarketingRound: jest.fn(),
  retryMarketingHandoff: jest.fn(),
  downloadMarketingHandoff: jest.fn(),
  getMarketingRound: jest.fn(),
  evaluateMarketingRound: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
const blobTransfer = {
  downloadBlob: jest.fn(async () => new Blob(['image'], { type: 'image/png' })),
};
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => blobTransfer,
}));
let current: MarketingRound | null;
const project =
  fixture.project as components['schemas']['MarketingProjectDetail'];
const ok = (data: unknown) => ({ kind: 'ok', data });
function show() {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <MarketingExecution project={project} />
    </SWRConfig>
  );
}
beforeEach(() => {
  jest.clearAllMocks();
  URL.createObjectURL = jest.fn(() => 'blob:marketing-review');
  URL.revokeObjectURL = jest.fn();
  gateway.getMarketingAIQCQuote.mockResolvedValue(
    ok({
      available: false,
      unavailable_reason: 'nothing_to_check',
      provider: 'mock',
      model: 'marketing-qc-example-v1',
      execution_mode: 'mock',
      quantity: 0,
      maximum_cost: '0',
      currency: 'USD',
      quote_hash: 'q'.repeat(64),
      asset_version_ids: [],
    })
  );
  gateway.createGenerationAssetVersionDownloadIntent.mockResolvedValue(
    ok({ download_grant: { url: 'http://localhost/private-image.png' } })
  );
  Object.defineProperty(globalThis.crypto, 'randomUUID', {
    configurable: true,
    value: () => 'test-action-id',
  });
  current = JSON.parse(
    JSON.stringify(fixture.bound)
  ) as unknown as MarketingRound;
  gateway.getMarketingWorkflow.mockImplementation(async () =>
    ok({
      project_id: project.id,
      rounds: [],
      current,
      next_action: 'import_delivery_data',
    })
  );
  for (const name of [
    'freezeMarketingRound',
    'reviewMarketingCreative',
    'previewMarketingMetrics',
    'confirmMarketingMetrics',
    'closeMarketingRound',
    'retryMarketingHandoff',
  ] as const)
    gateway[name].mockImplementation(async () =>
      ok({ id: current?.id, data: current })
    );
});

test('freezes explicit delivery dates and a structured final-checkpoint protocol', async () => {
  current = null;
  show();
  fireEvent.click(
    await screen.findByRole('button', {
      name: 'Confirm rules and review creatives',
    })
  );
  await waitFor(() => expect(gateway.freezeMarketingRound).toHaveBeenCalled());
  const params = gateway.freezeMarketingRound.mock.calls[0][0];
  expect(params.projectId).toBe(project.id);
  expect(params.expectedRevision).toBe(project.revision);
  expect(params.body.protocol).toMatchObject({
    checkpoint: 'end_of_window',
    min_days: 7,
    attribution_lag_days: 8,
  });
  expect(params.body.execution_mode).toBe('byok');
});

test('withdrawn export eligibility blocks download while delivery evidence can still be returned', async () => {
  current!.handoff = { ...current!.handoff!, quality_current: false };
  show();
  expect(
    await screen.findByText(messages['marketing-export-quality-changed'])
  ).toBeInTheDocument();
  expect(
    screen.getByRole('button', { name: 'Download Meta package' })
  ).toBeDisabled();
  expect(
    screen.queryByText(messages['marketing-manual-meta-instructions'])
  ).not.toBeInTheDocument();
  expect(
    screen.getByLabelText('Daily Meta CSV (up to 256 KB)')
  ).toBeInTheDocument();
  expect(gateway.downloadMarketingHandoff).not.toHaveBeenCalled();
});

test('a cached export link disappears when refreshed quality eligibility is withdrawn', async () => {
  gateway.downloadMarketingHandoff.mockImplementation(async () => {
    current!.handoff = { ...current!.handoff!, quality_current: false };
    return ok({
      download_grant: { url: 'https://download.example.test/historical.zip' },
    });
  });
  show();
  fireEvent.click(
    await screen.findByRole('button', { name: 'Download Meta package' })
  );
  await screen.findByText(messages['marketing-export-quality-changed']);
  expect(
    screen.queryByRole('link', {
      name: messages['marketing-open-zip-download'],
    })
  ).not.toBeInTheDocument();
});

test('a previous closed round can append an evaluation without replacing the current round', async () => {
  const past = JSON.parse(JSON.stringify(fixture.evaluated)) as MarketingRound;
  past.id = 'past-round';
  past.state = 'closed';
  past.close = {
    evaluation_id: past.evaluation?.id ?? null,
    plan_hash: past.snapshot.plan_hash,
    execution_mode: past.execution_mode,
    closed_at: '2025-01-20T00:00:00Z',
    reason: 'insufficient_evidence',
    note: 'Original closing record',
  };
  past.evaluation!.current = false;
  gateway.getMarketingWorkflow.mockImplementation(async () =>
    ok({
      project_id: project.id,
      rounds: [
        { id: past.id, ordinal: 1, state: 'closed', close: past.close },
        { id: current!.id, ordinal: 2, state: current!.state, close: null },
      ],
      current,
    })
  );
  gateway.getMarketingRound.mockImplementation(async () =>
    ok({ id: past.id, data: past })
  );
  gateway.evaluateMarketingRound.mockImplementation(async () =>
    ok({
      id: past.id,
      data: {
        ...past,
        revision: past.revision + 1,
        evaluation: {
          ...past.evaluation!,
          id: 'new-past-evaluation',
          current: true,
          threshold: '123.456',
        },
      },
    })
  );
  show();
  fireEvent.click(await screen.findByText(messages['marketing-round-history']));
  fireEvent.click(
    screen.getByRole('button', {
      name: messages['marketing-view-round-evidence'],
    })
  );
  await screen.findByText('Original closing record');
  fireEvent.click(
    screen.getByText(messages['marketing-view-delivery-records'])
  );
  expect(screen.getByLabelText('Daily Meta CSV (up to 256 KB)')).toBeVisible();
  expect(
    screen.queryByText(messages['marketing-build-meta-package'])
  ).not.toBeInTheDocument();
  fireEvent.click(
    screen.getByRole('button', {
      name: messages['marketing-reevaluate-recorded-data'],
    })
  );
  await waitFor(() =>
    expect(gateway.evaluateMarketingRound).toHaveBeenCalledWith(
      expect.objectContaining({
        roundId: past.id,
        expectedRevision: past.revision,
      })
    )
  );
  await waitFor(() =>
    expect(
      screen.queryByText(/This decision is historical/)
    ).not.toBeInTheDocument()
  );
  expect(screen.getByText('Original closing record')).toBeInTheDocument();
  expect(current!.id).not.toBe(past.id);
});

test('unknown semantic checks block quality approval until each is reviewed', async () => {
  current = {
    ...current!,
    state: 'reviewing',
    handoff: null,
    bindings: [],
    batches: [],
    evaluation: null,
    assets: [{ ...current!.assets[0], quality: null, ready: false }],
  };
  show();
  fireEvent.click(
    await screen.findByRole('button', { name: '使用情境演示 · Execution 1' })
  );
  fireEvent.click(
    await screen.findByText('Review manually without AI (optional fallback)')
  );
  const button = await screen.findByRole('button', { name: 'Approve quality' });
  expect(button).toBeDisabled();
  // A standard check is named by its copy, a plan check by its text (#308).
  for (const [key, text] of Object.entries(current.assets[0].required_checks))
    fireEvent.change(
      screen.getByLabelText(
        key.startsWith('intent_')
          ? text
          : (messages as Record<string, string>)[`marketing-ai-qc-check-${key}`]
      ),
      { target: { value: 'pass' } }
    );
  fireEvent.change(
    screen.getByLabelText('What you checked or what needs correction'),
    {
      target: {
        value: 'Compared actual image and copy with the frozen proposition.',
      },
    }
  );
  fireEvent.click(button);
  await waitFor(() =>
    expect(gateway.reviewMarketingCreative).toHaveBeenCalled()
  );
  expect(gateway.reviewMarketingCreative.mock.calls[0][0].body).toMatchObject({
    asset_version_id: current.assets[0].asset_version_id,
    quality: 'approved',
  });
});

test('CSV preflight precedes confirmation and retains explicit simulation provenance', async () => {
  show();
  const file = new File(['date,ad_id,spend\n2025-01-01,1000,10'], 'meta.csv', {
    type: 'text/csv',
  });
  Object.defineProperty(file, 'text', {
    value: async () => 'date,ad_id,spend\n2025-01-01,1000,10',
  });
  fireEvent.change(
    await screen.findByLabelText('Daily Meta CSV (up to 256 KB)'),
    { target: { files: [file] } }
  );
  fireEvent.click(
    screen.getByRole('checkbox', { name: /I confirm this file/ })
  );
  await waitFor(() =>
    expect(
      screen.getByRole('button', { name: 'Preview data and matches' })
    ).toBeEnabled()
  );
  gateway.previewMarketingMetrics.mockImplementation(async () => {
    current = JSON.parse(
      JSON.stringify(fixture.preview)
    ) as unknown as MarketingRound;
    return ok({ id: current.id, data: current });
  });
  fireEvent.click(
    screen.getByRole('button', { name: 'Preview data and matches' })
  );
  await waitFor(() =>
    expect(gateway.previewMarketingMetrics).toHaveBeenCalled()
  );
  expect(gateway.confirmMarketingMetrics).not.toHaveBeenCalled();
  expect(gateway.previewMarketingMetrics.mock.calls[0][0].body).toMatchObject({
    source_mode: 'controlled_mock',
    currency: 'USD',
    account_timezone: 'UTC',
    attribution_window: '7d_click_1d_view',
  });
  gateway.confirmMarketingMetrics.mockImplementation(async () => {
    current = {
      ...current!,
      batches: current!.batches.map((batch) => ({
        ...batch,
        state: 'confirmed',
      })),
    };
    return ok({ id: current.id, data: current });
  });
  fireEvent.click(
    await screen.findByRole('button', { name: 'Confirm this data import' })
  );
  await waitFor(() =>
    expect(gateway.confirmMarketingMetrics).toHaveBeenCalled()
  );
  await waitFor(() =>
    expect(
      screen.getByRole('checkbox', { name: /I confirm this file/ })
    ).not.toBeChecked()
  );
  expect(
    screen.getByRole('button', { name: 'Preview data and matches' })
  ).toBeDisabled();
  expect(
    (screen.getByLabelText('Daily Meta CSV (up to 256 KB)') as HTMLInputElement)
      .files
  ).toHaveLength(0);
});

test('a failed workflow refresh preserves the selected CSV and context confirmation', async () => {
  show();
  const file = new File(
    ['date,ad_id,spend\n2025-01-01,1000,10'],
    'retained.csv',
    {
      type: 'text/csv',
    }
  );
  Object.defineProperty(file, 'text', {
    value: async () => 'date,ad_id,spend\n2025-01-01,1000,10',
  });
  fireEvent.change(
    await screen.findByLabelText('Daily Meta CSV (up to 256 KB)'),
    {
      target: { files: [file] },
    }
  );
  fireEvent.click(
    screen.getByRole('checkbox', { name: /I confirm this file/ })
  );
  await waitFor(() =>
    expect(
      screen.getByRole('button', { name: 'Preview data and matches' })
    ).toBeEnabled()
  );
  gateway.downloadMarketingHandoff.mockImplementation(async () => {
    gateway.getMarketingWorkflow.mockRejectedValue(
      new Error('isolated API unavailable')
    );
    return ok({
      download_grant: { url: 'https://download.example.test/historical.zip' },
    });
  });
  fireEvent.click(
    screen.getByRole('button', { name: 'Download Meta package' })
  );
  await screen.findByText(messages['marketing-workflow-load-failed']);
  const input = screen.getByLabelText(
    'Daily Meta CSV (up to 256 KB)'
  ) as HTMLInputElement;
  expect(input.files?.[0]).toBe(file);
  expect(
    screen.getByRole('checkbox', { name: /I confirm this file/ })
  ).toBeChecked();
  expect(
    screen.getByRole('button', { name: 'Preview data and matches' })
  ).toBeDisabled();
  expect(
    screen.queryByRole('link', {
      name: messages['marketing-open-zip-download'],
    })
  ).not.toBeInTheDocument();
  gateway.getMarketingWorkflow.mockImplementation(async () =>
    ok({ project_id: project.id, rounds: [], current })
  );
  fireEvent.click(
    screen.getAllByRole('button', { name: messages['marketing-refresh'] })[0]
  );
  await waitFor(() =>
    expect(
      screen.getByRole('button', { name: 'Preview data and matches' })
    ).toBeEnabled()
  );
  expect(
    (screen.getByLabelText('Daily Meta CSV (up to 256 KB)') as HTMLInputElement)
      .files?.[0]
  ).toBe(file);
  expect(gateway.previewMarketingMetrics).not.toHaveBeenCalled();
});

test('shows scoped business evidence and closes without forcing a winner', async () => {
  current = JSON.parse(
    JSON.stringify(fixture.evaluated)
  ) as unknown as MarketingRound;
  show();
  expect(
    await screen.findByText(
      'Controlled simulation; unvalidated marketing evidence.'
    )
  ).toBeInTheDocument();
  expect(
    screen.getByText('0 exact ads supported by real delivery data.')
  ).toBeInTheDocument();
  expect(screen.getAllByText('This tested group of combinations')).toHaveLength(
    2
  );
  expect(screen.getAllByText('This exact ad')).toHaveLength(5);
  fireEvent.click(screen.getByText('End this small round'));
  fireEvent.change(
    screen.getByLabelText('What is known, unknown and worth doing next'),
    {
      target: {
        value: 'Simulation completed; real delivery is still unknown.',
      },
    }
  );
  fireEvent.click(
    screen.getByRole('button', { name: 'Save conclusion and end round' })
  );
  await waitFor(() => expect(gateway.closeMarketingRound).toHaveBeenCalled());
  expect(gateway.closeMarketingRound.mock.calls[0][0].body.reason).toBe(
    'insufficient_evidence'
  );
});

test('a closed round without an evaluation still respects the original big-round limit', async () => {
  current = {
    ...current!,
    state: 'closed',
    evaluation: null,
    close: {
      evaluation_id: null,
      plan_hash: current!.snapshot.plan_hash,
      execution_mode: current!.execution_mode,
      closed_at: '2025-01-20T00:00:00Z',
      reason: 'insufficient_evidence',
      note: 'No delivery; round count exhausted.',
    },
    continuation: {
      can_plan: false,
      remaining_budget: '300',
      remaining_rounds: 0,
      limit_reached: true,
      completed: false,
    },
  };
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <MarketingExecution
        project={project}
        onNextPlan={async () => {
          throw new Error('must not plan beyond the original limit');
        }}
      />
    </SWRConfig>
  );
  expect(
    await screen.findByText(
      /The original big-round budget or round limit has been reached/
    )
  ).toBeInTheDocument();
  expect(
    screen.queryByRole('button', { name: 'Draft next plan for review' })
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole('button', { name: 'Confirm rules and review creatives' })
  ).not.toBeInTheDocument();
});

// #310: the project read adds an inferred language to a plan from before J-02;
// the round froze the plan without it.
function endedOldPlanRound() {
  const ended = JSON.parse(
    JSON.stringify(fixture.evaluated)
  ) as unknown as MarketingRound;
  return {
    ...ended,
    state: 'closed',
    evaluation: { ...ended.evaluation!, budget_exhausted: false },
    close: {
      evaluation_id: ended.evaluation!.id,
      plan_hash: ended.snapshot.plan_hash,
      execution_mode: ended.execution_mode,
      closed_at: '2025-01-20T00:00:00Z',
      reason: 'insufficient_evidence',
      note: 'Simulated delivery only.',
    },
  } as MarketingRound;
}
const projectedOldPlan = (row: MarketingRound) => ({
  ...project,
  revision: row.plan_revision,
  plan: {
    ...row.snapshot.plan,
    ad_language: 'en-US',
    language_confirmation_required: true,
    strategy_language: 'zh-CN',
  } as unknown as Project['plan'],
});
type Project = components['schemas']['MarketingProjectDetail'];

test("an old plan's ended round offers the next plan and asks for its advertising language (#310)", async () => {
  current = endedOldPlanRound();
  expect(current.snapshot.plan.ad_language).toBeUndefined();
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <MarketingExecution
        project={projectedOldPlan(current)}
        onNextPlan={jest.fn(async () => undefined)}
      />
    </SWRConfig>
  );
  expect(
    await screen.findByRole('button', { name: 'Draft next plan for review' })
  ).toBeInTheDocument();
  expect(screen.getByLabelText('Advertising language')).toHaveValue('en-US');
});

test("a newer plan than the ended round's does not offer that round's next plan (#310)", async () => {
  current = endedOldPlanRound();
  const old = projectedOldPlan(current);
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <MarketingExecution
        project={{
          ...old,
          revision: old.revision + 1,
          plan: {
            ...old.plan!,
            question: 'A newer question',
            ad_language: 'zh-CN',
            language_confirmation_required: false,
          },
        }}
        onNextPlan={jest.fn(async () => undefined)}
      />
    </SWRConfig>
  );
  expect(
    await screen.findByRole('heading', {
      name: messages['marketing-delivery-and-results'],
    })
  ).toBeInTheDocument();
  expect(
    screen.queryByRole('button', { name: 'Draft next plan for review' })
  ).not.toBeInTheDocument();
});

// #367: a plan drafted since #222 / #228 (and #273, when `cited`). The mock
// reads the round as the server does: by the parameters the page sends.
function endedNewPlanRound(cited: boolean) {
  const stored = storedPlan(cited);
  const ended = endedOldPlanRound();
  gateway.getMarketingWorkflow.mockImplementation(async (params) =>
    ok({
      project_id: project.id,
      rounds: [],
      current: {
        ...ended,
        snapshot: { ...ended.snapshot, plan: roundRead(stored, params) },
      },
      next_action: 'prepare_next_plan',
    })
  );
  return {
    ...project,
    revision: ended.plan_revision,
    plan: stored as unknown as Project['plan'],
  };
}
function showNextPlan(document: Project) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <MarketingExecution
        project={document}
        onNextPlan={jest.fn(async () => undefined)}
      />
    </SWRConfig>
  );
}

test.each([
  ['review, style and competitor review', true],
  ['review and style', false],
])(
  "a new plan's ended round offers the next plan (#367: %s)",
  async (_, cited) => {
    const { container } = showNextPlan(endedNewPlanRound(cited));
    expect(
      await screen.findByRole('button', { name: 'Draft next plan for review' })
    ).toBeInTheDocument();
    expect(container.querySelector('#marketing-next-plan')).toBeInTheDocument();
    // The frozen plan has its advertising language; there is none to choose.
    expect(
      screen.queryByLabelText('Advertising language')
    ).not.toBeInTheDocument();
    expect(gateway.getMarketingWorkflow.mock.calls[0][0]).toMatchObject({
      includePlanReview: true,
      includeBrandStyle: true,
    });
    expect(gateway.getMarketingWorkflow.mock.calls[0][0]).not.toHaveProperty(
      'includeCompetitorReview'
    );
  }
);

test.each([
  [
    'a newer plan',
    (p: Project) => ({
      ...p,
      revision: p.revision + 1,
      plan: { ...p.plan!, question: 'A newer question' },
    }),
  ],
  [
    'no plan after the product information changed',
    (p: Project) => ({
      ...p,
      revision: p.revision + 1,
      state: 'input' as const,
      understanding: null,
      plan: null,
    }),
  ],
])(
  "%s does not offer the new plan's ended round's next plan (#367)",
  async (_, change) => {
    const { container } = showNextPlan(change(endedNewPlanRound(true)));
    expect(
      await screen.findByRole('heading', {
        name: messages['marketing-delivery-and-results'],
      })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Draft next plan for review' })
    ).not.toBeInTheDocument();
    expect(
      container.querySelector('#marketing-next-plan')
    ).not.toBeInTheDocument();
  }
);

test('no report is shown as unknown delivery instead of zero observed purchases or spend', async () => {
  current = JSON.parse(
    JSON.stringify(fixture.evaluated)
  ) as unknown as MarketingRound;
  current.execution_mode = 'byok';
  current.evaluation!.evidence_status = 'not_supplied';
  current.evaluation!.results = current.evaluation!.results.map((result) => ({
    ...result,
    business_status: 'insufficient',
    estimate: null,
    interval_low: null,
    interval_high: null,
    purchases_reported: false,
  }));
  show();
  expect(
    (await screen.findAllByText(/No delivery data supplied/))[0]
  ).toBeInTheDocument();
  expect(
    screen.getByText(/No media spend has been reported/)
  ).toBeInTheDocument();
  expect(
    screen.getAllByText('Delivery days, impressions and purchases are unknown.')
  ).toHaveLength(7);
  expect(screen.queryByText(/0 days · 0 impressions/)).not.toBeInTheDocument();
});

test('an optional purchase column omitted from a CTR report stays unknown', async () => {
  current = JSON.parse(
    JSON.stringify(fixture.evaluated)
  ) as unknown as MarketingRound;
  current.evaluation!.metric = 'ctr';
  current.evaluation!.evidence_status = 'user_supplied_delivery';
  current.evaluation!.results = current.evaluation!.results.map((result) => ({
    ...result,
    purchases_reported: false,
  }));
  show();
  expect(await screen.findAllByText(/Unknown purchases/)).toHaveLength(7);
});

test('changed evidence preserves the historical decision and exposes reevaluation without paid interpretation', async () => {
  current = JSON.parse(
    JSON.stringify(fixture.evaluated)
  ) as unknown as MarketingRound;
  current.execution_mode = 'byok';
  current.evaluation!.current = false;
  current.evaluation!.qualified_asset_version_ids = [
    'previous-approved-version',
  ];
  current.evaluation!.confirmed_combinations = [
    {
      combination_id: 'previous-combination',
      combination_key: 'test-key',
      current_asset_version_id: 'previous-approved-version',
      supporting_evaluations: ['test-evaluation'],
      scope: 'repeatable_under_recorded_conditions',
    },
  ];
  current.explanation = {
    provider: 'mock',
    model: 'test',
    maximum_cost: '0',
    id: 'past-interpretation',
    state: 'succeeded',
    current: false,
    execution_mode: 'byok',
    error_code: null,
    data: null,
  };
  show();
  expect(
    await screen.findByText(/This decision is historical/)
  ).toBeInTheDocument();
  expect(
    screen.getByText('View the previous decision · not current evidence')
  ).toBeInTheDocument();
  expect(
    screen.getByRole('button', { name: 'Evaluate confirmed delivery data' })
  ).toBeVisible();
  expect(
    screen.queryByRole('button', { name: 'Interpret these results with AI' })
  ).not.toBeInTheDocument();
  fireEvent.click(screen.getByText('End this small round'));
  expect(
    screen.queryByRole('option', { name: 'Reusable combinations confirmed' })
  ).not.toBeInTheDocument();
});

test('an idle review stops polling while an active background job continues polling', async () => {
  jest.useFakeTimers();
  try {
    current!.state = 'reviewing';
    let interval = 0;
    gateway.getMarketingWorkflow.mockImplementation(async () =>
      ok({
        project_id: project.id,
        rounds: [],
        current,
        next_action: 'review_creatives',
        poll_after_ms: interval,
      })
    );
    const view = show();
    await act(async () => {
      await jest.advanceTimersByTimeAsync(50);
    });
    const initial = gateway.getMarketingWorkflow.mock.calls.length;
    await act(async () => {
      await jest.advanceTimersByTimeAsync(10000);
    });
    expect(gateway.getMarketingWorkflow).toHaveBeenCalledTimes(initial);
    view.unmount();
    interval = 3000;
    show();
    await act(async () => {
      await jest.advanceTimersByTimeAsync(50);
    });
    const active = gateway.getMarketingWorkflow.mock.calls.length;
    await act(async () => {
      await jest.advanceTimersByTimeAsync(3100);
    });
    expect(gateway.getMarketingWorkflow.mock.calls.length).toBeGreaterThan(
      active
    );
  } finally {
    jest.useRealTimers();
  }
});

/** Five images through both AI checks, each waiting for quality review. */
const checkedRound = () =>
  JSON.parse(JSON.stringify(qcFixture.row)) as unknown as MarketingRound;

/** The frame of #307: the last paired check still runs on the board. */
function lastPairedCheck(state: 'running' | 'uncertain') {
  const round = checkedRound();
  const asset = round.assets[round.assets.length - 1];
  asset.paired_ai_qc = { ...asset.paired_ai_qc!, state, result: null };
  const node = boardExecutions(round).find(
    (e) => e.asset_version_id === asset.asset_version_id
  )!;
  Object.assign(node, {
    stage: 'paired_qc_running',
    gap: 'paired_qc',
    next_action: 'none',
    paired_ai_qc: { ...node.paired_ai_qc!, state },
  });
  return round;
}

/** Serves `frames` in order, then the last one again, always saying idle. */
function serveIdle(...frames: MarketingRound[]) {
  gateway.getMarketingWorkflow.mockImplementation(async () =>
    ok({
      project_id: project.id,
      rounds: [],
      current: frames.length > 1 ? frames.shift() : frames[0],
      next_action: 'review_creatives',
      poll_after_ms: 0,
    })
  );
}

async function advance(ms: number) {
  await act(async () => {
    await jest.advanceTimersByTimeAsync(ms);
  });
}

test('a frame still showing a running check polls again though the server says idle (#307)', async () => {
  jest.useFakeTimers();
  try {
    serveIdle(lastPairedCheck('running'), checkedRound());
    const onFocusChange = jest.fn();
    render(
      <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
        <MarketingExecution project={project} onFocusChange={onFocusChange} />
      </SWRConfig>
    );
    await advance(50);
    const stale = gateway.getMarketingWorkflow.mock.calls.length;
    // The running check is under way, not missing (#385).
    expect(
      screen.getAllByText(messages['marketing-board-stage-paired_qc_running'])
    ).toHaveLength(1);
    expect(
      screen.queryByText(messages['marketing-board-gap-paired_qc'])
    ).not.toBeInTheDocument();
    expect(onFocusChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ step: 'review', count: 4 })
    );
    await advance(3100);
    expect(gateway.getMarketingWorkflow.mock.calls.length).toBeGreaterThan(
      stale
    );
    expect(onFocusChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ step: 'review', count: 5 })
    );
    expect(
      screen.queryByText(messages['marketing-board-gap-paired_qc'])
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(messages['marketing-board-stage-paired_qc_running'])
    ).not.toBeInTheDocument();
    // The finished frame runs nothing, so the server's idle stands.
    const finished = gateway.getMarketingWorkflow.mock.calls.length;
    await advance(10000);
    expect(gateway.getMarketingWorkflow).toHaveBeenCalledTimes(finished);
  } finally {
    jest.useRealTimers();
  }
});

test.each<[string, (round: MarketingRound) => void]>([
  [
    'a queued single-image check',
    (round) => {
      round.assets[0].ai_qc = {
        ...round.assets[0].ai_qc!,
        state: 'queued',
        result: null,
      };
    },
  ],
  [
    'a queued export',
    (round) => {
      round.handoff = { ...fixture.bound.handoff, state: 'queued' };
    },
  ],
  [
    'a running export',
    (round) => {
      round.handoff = { ...fixture.bound.handoff, state: 'running' };
    },
  ],
  [
    'a running explanation',
    (round) => {
      round.explanation = {
        id: 'mex_running',
        state: 'running',
        execution_mode: 'mock',
        provider: 'mock',
        model: 'mock',
        maximum_cost: '0',
        error_code: null,
        current: true,
        data: null,
      };
    },
  ],
  [
    'a queued image edit',
    (round) => {
      round.image_edits = [
        {
          id: 'mie_queued',
          operation_id: 'op_queued',
          source_asset_version_id: round.assets[0].asset_version_id,
          result_asset_version_id: null,
          state: 'queued',
          error_code: null,
        },
      ];
    },
  ],
  [
    'an image being generated',
    (round) => {
      boardExecutions(round)[0].stage = 'generating';
    },
  ],
])('%s keeps polling though the server says idle', async (_, running) => {
  jest.useFakeTimers();
  try {
    const round = checkedRound();
    running(round);
    serveIdle(round);
    show();
    await advance(50);
    const initial = gateway.getMarketingWorkflow.mock.calls.length;
    await advance(3100);
    expect(gateway.getMarketingWorkflow.mock.calls.length).toBeGreaterThan(
      initial
    );
  } finally {
    jest.useRealTimers();
  }
});

test.each<[string, () => MarketingRound]>([
  // An uncertain call waits for a person to resolve it, not for a poll.
  ['an uncertain paired check', () => lastPairedCheck('uncertain')],
  ['every check finished', checkedRound],
])('%s stops polling when the server says idle', async (_, frame) => {
  jest.useFakeTimers();
  try {
    serveIdle(frame());
    show();
    await advance(50);
    const initial = gateway.getMarketingWorkflow.mock.calls.length;
    await advance(10000);
    expect(gateway.getMarketingWorkflow).toHaveBeenCalledTimes(initial);
  } finally {
    jest.useRealTimers();
  }
});

test('freezes a selected attribution window and enforces its minimum waiting period', async () => {
  current = null;
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <MarketingExecution
        project={project}
        reporting={{
          mock_available: true,
          paid: {
            provider: 'mock',
            model: 'mock',
            available: false,
            maximum_cost: '0',
            currency: 'USD',
            quote_hash: '',
            unavailable_reason: null,
          },
          media_currencies: ['USD', 'HKD'],
          attribution_windows: ['1d_click', '7d_click_1d_view'],
          attribution_min_lag_days: { '1d_click': 2, '7d_click_1d_view': 8 },
        }}
      />
    </SWRConfig>
  );
  fireEvent.change(await screen.findByLabelText('Attribution window'), {
    target: { value: '1d_click' },
  });
  fireEvent.change(screen.getByLabelText('Days to wait after delivery'), {
    target: { value: '2' },
  });
  fireEvent.change(screen.getByLabelText('Attribution window'), {
    target: { value: '7d_click_1d_view' },
  });
  expect(screen.getByLabelText('Days to wait after delivery')).toHaveValue(8);
  fireEvent.change(screen.getByLabelText('Attribution window'), {
    target: { value: '1d_click' },
  });
  fireEvent.click(
    screen.getByRole('button', { name: 'Confirm rules and review creatives' })
  );
  await waitFor(() =>
    expect(gateway.freezeMarketingRound).toHaveBeenCalledWith(
      expect.objectContaining({
        body: expect.objectContaining({ attribution_window: '1d_click' }),
      })
    )
  );
});

test('the board replaces the flat creative list and its drawer mounts the action the server names next', async () => {
  show();
  const tile = await screen.findByRole('button', {
    name: '使用情境演示 · Execution 1',
  });
  expect(gateway.getMarketingWorkflow.mock.calls[0][0]).toMatchObject({
    includeBoard: true,
    includeExecutionPlan: true,
    includeBoardThumbnails: true,
  });
  expect(
    screen.getByRole('heading', { name: current!.board!.question })
  ).toBeInTheDocument();
  fireEvent.click(tile);
  const drawer = await screen.findByRole('dialog', { name: 'Image details' });
  // Bound but without returned data: the one next step is importing it.
  expect(drawer).toHaveTextContent('No delivery data yet');
  expect(drawer).toHaveTextContent('Import delivery data');
  expect(
    within(drawer).getByLabelText('Daily Meta CSV (up to 256 KB)')
  ).toBeInTheDocument();
  // Exported rounds keep per-image tools out of the drawer.
  expect(within(drawer).queryByText('More actions')).not.toBeInTheDocument();
});

test('a round without a server board says so instead of rebuilding one in the browser', async () => {
  delete (current as { board?: unknown }).board;
  show();
  expect(
    await screen.findByText(messages['marketing-board-unavailable'])
  ).toBeInTheDocument();
  expect(
    screen.queryByRole('button', { name: '使用情境演示 · Execution 1' })
  ).not.toBeInTheDocument();
});

test('a package written in another language says so; one in the interface language does not (#255)', async () => {
  current!.handoff = { ...current!.handoff!, language: 'zh-CN' };
  const { unmount } = show();
  expect(
    await screen.findByText(messages['marketing-handoff-language-zh-CN'])
  ).toBeInTheDocument();
  expect(gateway.getMarketingWorkflow.mock.calls[0][0]).toMatchObject({
    includeTextLanguage: true,
  });
  unmount();
  current!.handoff = { ...current!.handoff!, language: 'en' };
  show();
  await screen.findByText(messages['marketing-manual-meta-instructions']);
  expect(
    screen.queryByText(messages['marketing-handoff-language-en'])
  ).not.toBeInTheDocument();
});

test('CSV preflight issues are worded from their code and params (#255)', async () => {
  current!.batches = [
    {
      id: 'mmb_preview',
      state: 'preview',
      raw_hash: 'h'.repeat(64),
      metadata: {
        filename: 'meta.csv',
        ad_account_id: '123',
        account_timezone: 'UTC',
        currency: 'USD',
        attribution_window: '7d_click_1d_view',
        source_mode: 'controlled_mock',
        field_mapping: {},
        resolved_mapping: {},
      },
      row_count: 2,
      issues: [
        {
          row: 9,
          field: 'date',
          code: 'outside_round_dates',
          params: { start: '2025-01-01', end: '2025-01-07' },
        },
        // A file-level issue reads without a row number.
        { row: 0, field: 'ad_account_id', code: 'account_or_binding_missing' },
      ],
      replaces_batch_id: null,
    },
  ];
  show();
  expect(
    await screen.findByText(
      'Row 9 · Date: The date is outside this round (2025-01-01 to 2025-01-07).'
    )
  ).toBeInTheDocument();
  expect(
    screen.getByText('Ad account: Match the Meta account and Ad IDs first.')
  ).toBeInTheDocument();
});
