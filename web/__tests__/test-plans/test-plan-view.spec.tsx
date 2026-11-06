/**
 * @jest-environment jsdom
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { TestPlanView } from '@/components/test-plans/TestPlanView';

const mockPush = jest.fn();
const mockGateway = {
  getTestPlan: jest.fn(),
  reviseTestPlan: jest.fn(),
  createTestPlanRun: jest.fn(),
  listGenerationRunItems: jest.fn(),
};
const mockNextRound = jest.fn<null, [unknown]>(() => null);

jest.mock('next/router', () => ({
  useRouter: () => ({ push: mockPush, query: { slug: 'acme' } }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: 'OWNER' } }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/components/test-plans/GenerationDefaultsBanner', () => ({
  GenerationDefaultsBanner: () => null,
  PROVIDER_CREDENTIALS_ANCHOR: 'provider-credentials',
}));
jest.mock('@/components/test-plans/PlanNextRoundSlot', () => ({
  PlanNextRoundSlot: (props: unknown) => mockNextRound(props),
}));
jest.mock('@/hooks/assets/useAssetImageUrl', () => ({
  useAssetImageUrl: (id: string) => ({
    blobUrl: `blob:${id}`,
    isLoading: false,
    problem: undefined,
    source: 'thumbnail',
  }),
}));

const meta = { etag: null, retryAt: null, location: null };
const MONEY = (amount: string) => ({ amount, currency: 'USD' });
const UNTESTED = (summary: string) => ({
  count: 2,
  space_count: 6,
  constraint_excluded_count: 0,
  summary,
});

function direction(id: string, title: string, overrides: any = {}) {
  return {
    direction_id: id,
    ordinal: Number(id.slice(-1)),
    title,
    what_it_tests: `${title}是否更打动通勤族`,
    hypothesis: '',
    scene_description: `${title}的画面`,
    copy: { headline: `${title}标题`, primary_text: `${title}正文` },
    factors: [
      { key: 'scene', label: '场景', values: [{ id: 'night', label: '夜晚' }] },
    ],
    factor_values: { scene: ['night'] },
    combination_count: 1,
    combination_signatures: [`direction=${id}|scene=night`],
    selected_in: { sample: 1, small: 1, full: 1 },
    removed: false,
    user_added: false,
    ...overrides,
  };
}

function planData(overrides: any = {}) {
  return {
    plan_id: 'atp_1',
    operation_id: 'op_1',
    status: 'ready',
    revision: 4,
    product_id: 'prod_1',
    product_entry_id: 'lib_1',
    product_version_id: 'libver_1',
    product_name: 'VoltNest',
    goal: 'explore',
    audience: { text: '刚提车的通勤族' },
    scale: 'sample',
    factors: [
      {
        key: 'scene',
        label: '场景',
        values: [
          { id: 'night', label: '夜晚', proposed_by: 'ai' },
          { id: 'day', label: '白天', proposed_by: 'ai' },
        ],
      },
    ],
    directions: [
      direction('dir-1', '雨夜车库'),
      direction('dir-2', '周末露营'),
      direction('dir-3', '老小区', { removed: true }),
    ],
    stages: {
      sample: {
        scale: 'sample',
        combinations: 2,
        variants: 1,
        images: 2,
        max_cost: MONEY('0.34'),
        untested: UNTESTED('Samples'),
      },
      small: {
        scale: 'small',
        combinations: 2,
        variants: 2,
        images: 4,
        max_cost: MONEY('0.68'),
        untested: UNTESTED('小测未测到 0 个'),
      },
      full: {
        scale: 'full',
        combinations: 2,
        variants: 2,
        images: 4,
        max_cost: MONEY('0.68'),
        untested: UNTESTED(
          '共 6 个组合，本次测 4 个，未测到 2 个（「雨夜车库」2 个）。'
        ),
      },
    },
    expansion: {
      stage: 'full',
      scale: 'full',
      combinations: 1,
      reused_combinations: 1,
      variants: 2,
      images: 2,
      max_cost: MONEY('0.34'),
      sample_run_in_progress: false,
      already_run: false,
    },
    defaults: {
      provider: 'openai',
      model: 'gpt-image-2',
      execution_mode: 'byok',
      aspect_ratio: '1:1',
      variants: { sample: 1, small: 2, full: 2 },
      source: 'workspace',
      revision: 2,
      credential_bound: true,
      warnings: [],
    },
    runs: [],
    drafting: {},
    replayed: false,
    ...overrides,
  };
}

const SAMPLE_RUN = {
  stage: 'sample',
  target_scale: 'sample',
  status: 'submitted',
  plan_revision: 4,
  run_id: 'gr_1',
  operation_id: 'op_2',
  run_status: 'succeeded',
  items_total: 2,
  items_done: 2,
  items_failed: 0,
};

async function flush() {
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
}

function ok(data: unknown, kind = 'ok') {
  return { kind, status: kind === 'accepted' ? 202 : 200, data, meta };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockGateway.getTestPlan.mockResolvedValue(ok(planData()));
  mockGateway.listGenerationRunItems.mockResolvedValue(
    ok({ items: [], next_cursor: null })
  );
});

function card(title: string) {
  return screen
    .getAllByTestId('direction-card')
    .find((element) =>
      within(element).queryByRole('heading', { name: new RegExp(title) })
    )!;
}

/** Every control the legacy page used to write with (#337 removed them). */
function expectReadOnly() {
  for (const name of [
    /Edit direction/,
    /Delete direction/,
    /Restore direction/,
    'Add my own idea',
    'Make samples',
    'Expand to full test',
    'Set up the test again',
    /Import (delivery|more) results/,
  ]) {
    expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
  }
  expect(screen.queryAllByRole('radio')).toEqual([]);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.queryByTestId('full-adds-nothing')).not.toBeInTheDocument();
  expect(mockGateway.reviseTestPlan).not.toHaveBeenCalled();
  expect(mockGateway.createTestPlanRun).not.toHaveBeenCalled();
  expect(mockPush).not.toHaveBeenCalled();
}

function expectNotice() {
  const notice = screen.getByTestId('legacy-tests-notice');
  expect(notice).toHaveTextContent(
    'This older test flow has been retired: its tests can be viewed but no longer changed, generated or imported into.'
  );
  expect(
    within(notice).getByRole('link', {
      name: 'Start new tests in Marketing tests',
    })
  ).toHaveAttribute('href', '/w/acme/experiments');
}

describe('TestPlanView (read-only since #337)', () => {
  it('shows direction cards, what the plan covers and no internal ids', async () => {
    render(<TestPlanView planId="atp_1" />);
    await flush();

    expect(
      screen.getByRole('heading', { level: 1, name: 'Ad test for VoltNest' })
    ).toBeInTheDocument();
    expectNotice();
    expect(screen.getAllByTestId('direction-card')).toHaveLength(2);
    const first = within(card('雨夜车库'));
    expect(first.getByText('What this group tests')).toBeInTheDocument();
    expect(first.getByText('雨夜车库是否更打动通勤族')).toBeInTheDocument();
    expect(first.getByText('雨夜车库标题')).toBeInTheDocument();
    expect(first.getByText('Samples: 1 · Full test: 2')).toBeInTheDocument();
    expect(screen.getByText('Deleted directions (1)')).toBeInTheDocument();
    expect(screen.getAllByTestId('removed-direction')).toHaveLength(1);
    const totals = within(screen.getByTestId('plan-totals'));
    expect(totals.getByText('2 directions')).toBeInTheDocument();
    expect(totals.getByText('Samples first')).toBeInTheDocument();
    expect(screen.getByTestId('untested-summary')).toHaveTextContent(
      '未测到 2 个'
    );
    expectReadOnly();
    // Internal view is off and not rendered.
    expect(screen.queryByTestId('plan-internal-view')).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(
      /atp_|prod_|lib_|libver_|op_|dir-\d|direction=/
    );

    fireEvent.click(screen.getByRole('button', { name: 'Show internal view' }));
    expect(screen.getByTestId('plan-internal-view')).toHaveTextContent(
      'direction=dir-1|scene=night'
    );
  });

  it('puts each sample image on its direction card and shows the results, with no 展开完整测试', async () => {
    mockGateway.getTestPlan.mockResolvedValue(
      ok(planData({ runs: [SAMPLE_RUN] }))
    );
    mockGateway.listGenerationRunItems.mockResolvedValue(
      ok({
        items: [
          {
            id: 'ri_2',
            ordinal: 1,
            state: 'succeeded',
            combination_fingerprint: 'x',
            tags: { direction: 'dir-2', scene: 'night' },
            bound_asset_version_id: 'av_2',
            thumbnail: null,
          },
          {
            id: 'ri_1',
            ordinal: 0,
            state: 'succeeded',
            combination_fingerprint: 'y',
            tags: { direction: 'dir-1', scene: 'night' },
            bound_asset_version_id: 'av_1',
            thumbnail: null,
          },
        ],
        next_cursor: null,
      })
    );
    render(<TestPlanView planId="atp_1" />);
    await flush();

    expect(mockGateway.listGenerationRunItems).toHaveBeenCalledWith(
      expect.objectContaining({ runId: 'gr_1' })
    );
    const night = within(card('雨夜车库'));
    expect(night.getByText('Samples')).toBeInTheDocument();
    const link = night.getByRole('link', { name: 'View image 1 in Assets' });
    expect(link).toHaveAttribute(
      'href',
      '/w/acme/assets?run=gr_1&overlay=av_1'
    );
    expect(link.querySelector('img')).toHaveAttribute('src', 'blob:av_1');
    expect(
      within(card('周末露营')).getByRole('link', {
        name: 'View image 1 in Assets',
      })
    ).toHaveAttribute('href', '/w/acme/assets?run=gr_1&overlay=av_2');
    expect(
      screen.getByRole('link', { name: 'Review all in Assets' })
    ).toHaveAttribute('href', '/w/acme/assets?run=gr_1&reviewMode=1');
    expect(mockNextRound).toHaveBeenCalledWith(
      expect.objectContaining({ settledStage: 'sample' })
    );
    expectReadOnly();
  });

  it('shows a plan whose full test ran, read-only', async () => {
    mockGateway.getTestPlan.mockResolvedValue(
      ok(
        planData({
          runs: [
            SAMPLE_RUN,
            {
              ...SAMPLE_RUN,
              stage: 'full',
              target_scale: 'full',
              run_id: 'gr_2',
            },
          ],
        })
      )
    );
    render(<TestPlanView planId="atp_1" />);
    await flush();
    expect(screen.getByText('Full test')).toBeInTheDocument();
    expect(
      screen.getAllByRole('link', { name: 'Review all in Assets' })
    ).toHaveLength(2);
    expect(mockNextRound).toHaveBeenCalledWith(
      expect.objectContaining({ settledStage: 'full' })
    );
    expectReadOnly();
  });

  it('a failed Run says why, with no way to fix it in Settings', async () => {
    mockGateway.getTestPlan.mockResolvedValue(
      ok(
        planData({
          runs: [
            {
              ...SAMPLE_RUN,
              status: 'failed',
              run_status: 'failed',
              failure: {
                code: 'credential_required',
                message: 'workspace has no usable key',
                retryable: false,
              },
            },
          ],
        })
      )
    );
    render(<TestPlanView planId="atp_1" />);
    await flush();
    const alert = document.querySelector(
      '[data-problem-code="credential_required"]'
    ) as HTMLElement;
    expect(alert).toHaveTextContent(
      'The generation defaults use OpenAI, but the workspace has no usable key yet.'
    );
    expect(
      within(alert).queryByRole('link', { name: 'Go to Settings' })
    ).not.toBeInTheDocument();
    expect(alert).not.toHaveTextContent(
      'Ask the workspace Owner to handle this.'
    );
    expect(screen.queryByRole('link', { name: 'Go to Settings' })).toBeNull();
    expectReadOnly();
  });

  it('polls while drafting and shows the cards once ready', async () => {
    jest.useFakeTimers();
    try {
      mockGateway.getTestPlan
        .mockResolvedValueOnce(
          ok(planData({ status: 'drafting', directions: [], stages: {} }))
        )
        .mockResolvedValue(ok(planData()));
      render(<TestPlanView planId="atp_1" />);
      await flush();
      expect(
        screen.getByText('AI is drafting creative directions')
      ).toBeInTheDocument();
      await act(async () => {
        jest.advanceTimersByTime(1100);
      });
      await flush();
      expect(mockGateway.getTestPlan).toHaveBeenCalledTimes(2);
      expect(screen.getAllByTestId('direction-card')).toHaveLength(2);
    } finally {
      jest.useRealTimers();
    }
  });

  it('a failed draft says why, with no retry and nothing stashed for one', async () => {
    mockGateway.getTestPlan.mockResolvedValue(
      ok(
        planData({
          status: 'failed',
          directions: [],
          stages: {},
          goal: 'optimize',
          scale: 'small',
          seed_asset_version_ids: ['av_seed'],
          failure: {
            code: 'drafting_failed',
            message: 'AI 没能起草出测试方案，请重试。',
            retryable: true,
          },
        })
      )
    );
    render(<TestPlanView planId="atp_1" />);
    await flush();
    expect(
      screen.getByText('No plan could be drafted this time')
    ).toBeInTheDocument();
    // The failure reads by its code; the server's sentence is a diagnostic.
    expect(
      screen.getByText('The AI could not draft a test plan. Try again.')
    ).toBeInTheDocument();
    expect(screen.queryByText('AI 没能起草出测试方案，请重试。')).toBeNull();
    expect(screen.queryByText(/Drafting again makes new calls/)).toBeNull();
    expectNotice();
    expectReadOnly();
    expect(window.sessionStorage.getItem('products:acme:new-test')).toBeNull();
    expect(window.sessionStorage.getItem('tests:acme:new-setup')).toBeNull();
  });
});
