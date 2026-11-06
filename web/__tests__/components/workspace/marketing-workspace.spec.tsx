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
import { MarketingWorkspace } from '@/components/workspace/MarketingWorkspace';
import { retryTransientProblems } from '@/lib/domain-client/transient-problems';
import sample from '../../fixtures/marketing-plan.json';
import messages from '../../../locales/en/common.json';
import zhMessages from '../../../locales/zh-CN/common.json';
import { DefaultDomainGateway } from '@/lib/domain-client/gateway';
import { CsrfTokenManager } from '@/lib/domain-client/csrf';
import { IntentTracker } from '@/lib/domain-client/intent';
let mockMessages: object = messages;
jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string }) =>
      (
        (mockMessages as Record<string, string>)[key] ??
        options?.defaultValue ??
        key
      ).replace(/\{\{(\w+)\}\}/g, (_: string, name: string) => {
        // Like i18next: `replace` (how error copy is interpolated) wins.
        const vars = options as Record<string, unknown> | undefined;
        const replaced = vars?.replace as Record<string, unknown> | undefined;
        return String(replaced?.[name] ?? vars?.[name] ?? '');
      }),
  }),
}));

let query: Record<string, string> = {};
const replace = jest.fn().mockImplementation(async ({ query: next }) => {
  query = next;
  return true;
});
const gateway = {
  getMarketingCapabilities: jest.fn(),
  getGenerationCapabilities: jest.fn(),
  listMarketingProjects: jest.fn(),
  getMarketingProject: jest.fn(),
  getMarketingWorkflow: jest.fn(),
  createMarketingProject: jest.fn(),
  editMarketingProject: jest.fn(),
  startMarketingPlanning: jest.fn(),
  prepareMarketingGeneration: jest.fn(),
  listMarketingBrandKits: jest.fn(),
  listProducts: jest.fn(),
};
let mockLocale = 'en';
jest.mock('next/router', () => ({
  useRouter: () => ({
    query,
    replace,
    pathname: '/w/[slug]/experiments',
    locale: mockLocale,
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws_alpha', slug: 'alpha' } }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
jest.mock('@/components/products/ProductThumbnail', () => ({
  ProductThumbnail: () => null,
}));
jest.mock('@/components/library/references/ReferenceThumbnail', () => ({
  ReferenceThumbnail: ({ alt }: { alt: string }) => <span>{alt}</span>,
}));
jest.mock('@/components/workspace/GenerationComposer', () => ({
  GenerationComposer: ({ embeddedDraftId }: { embeddedDraftId: string }) => (
    <p>{embeddedDraftId}</p>
  ),
}));
const input = {
  id: 'mp_1',
  language_support: 'v1',
  plan_review_support: 'v1',
  brand_style_support: 'v1',
  revision: 1,
  state: 'input',
  source_text: 'Alpha 充电器',
  product_id: null,
  understanding: null,
  plan: null,
  latest_call: null,
  evidence_status: 'unvalidated',
  generation_draft_ids: [],
};
const ok = (data: unknown) => ({ kind: 'ok', data });
function show() {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <MarketingWorkspace />
    </SWRConfig>
  );
}
beforeEach(() => {
  jest.clearAllMocks();
  query = {};
  gateway.getMarketingWorkflow.mockResolvedValue(
    ok({
      project_id: 'mp_1',
      rounds: [],
      current: null,
      next_action: 'freeze_round',
    })
  );
  gateway.getMarketingCapabilities.mockResolvedValue(
    ok({
      paid: {
        provider: 'gemini',
        model: '',
        available: false,
        maximum_cost: '0',
        quote_hash: 'quote',
      },
    })
  );
  gateway.getGenerationCapabilities.mockResolvedValue(
    ok({
      products: [],
      provider_options: [
        {
          id: 'mock',
          label: 'Controlled Mock',
          default_model: 'mock-v1',
          available: true,
        },
      ],
    })
  );
  gateway.listMarketingProjects.mockResolvedValue(ok({ items: [] }));
  gateway.listMarketingBrandKits.mockResolvedValue(ok({ items: [] }));
  gateway.listProducts.mockResolvedValue(ok({ items: [] }));
  gateway.getMarketingProject.mockResolvedValue(ok(input));
});

test('saved tests load beyond fifty and search resets pagination without losing selection context', async () => {
  gateway.listMarketingProjects.mockImplementation(async ({ cursor, q }) =>
    ok(
      q
        ? {
            items: [
              {
                id: 'older',
                revision: 1,
                state: 'input',
                product_name: 'Older charger',
              },
            ],
            next_cursor: null,
          }
        : cursor
          ? {
              items: [
                {
                  id: 'older',
                  revision: 1,
                  state: 'input',
                  product_name: 'Older charger',
                },
              ],
              next_cursor: null,
            }
          : {
              items: Array.from({ length: 50 }, (_, i) => ({
                id: `p${i}`,
                revision: 1,
                state: 'input',
                product_name: `Recent ${i}`,
              })),
              next_cursor: 'page-two',
            }
    )
  );
  const view = show();
  fireEvent.click(
    await screen.findByRole('button', { name: 'Load more tests' })
  );
  expect(
    await screen.findByRole('button', { name: /Older charger/ })
  ).toBeVisible();
  expect(gateway.listMarketingProjects).toHaveBeenCalledWith(
    expect.objectContaining({ cursor: 'page-two', limit: 50 })
  );
  fireEvent.change(screen.getByLabelText('Search saved tests'), {
    target: { value: 'older' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Search' }));
  await waitFor(() => expect(query.q).toBe('older'));
  view.rerender(
    <SWRConfig value={{ dedupingInterval: 0 }}>
      <MarketingWorkspace />
    </SWRConfig>
  );
  await waitFor(() =>
    expect(gateway.listMarketingProjects).toHaveBeenCalledWith(
      expect.objectContaining({ q: 'older', cursor: undefined })
    )
  );
  expect(
    screen.queryByRole('button', { name: 'Load more tests' })
  ).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole('button', { name: /Older charger/ }));
  await waitFor(() =>
    expect(replace).toHaveBeenLastCalledWith(
      expect.objectContaining({
        query: { slug: 'alpha', q: 'older', test: 'older' },
      }),
      undefined,
      { shallow: true }
    )
  );
});

test('product information is saved before AI drafting and has no variable matrix', async () => {
  gateway.createMarketingProject.mockResolvedValue(ok(input));
  show();
  fireEvent.change(screen.getByLabelText('Product details'), {
    target: { value: 'Alpha 充电器，65W，多口' },
  });
  fireEvent.click(
    screen.getByRole('button', { name: 'Save product and start' })
  );
  await waitFor(() =>
    expect(gateway.createMarketingProject).toHaveBeenCalledTimes(1)
  );
  expect(
    gateway.createMarketingProject.mock.calls[0][0].body.source_text
  ).toContain('65W');
  expect(gateway.startMarketingPlanning).not.toHaveBeenCalled();
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
});

test('connection failure preserves the input and retries the same creation intent', async () => {
  gateway.createMarketingProject
    .mockResolvedValueOnce({
      kind: 'problem',
      problem: { code: 'domain_unavailable', title: 'domain unavailable' },
    })
    .mockResolvedValueOnce(ok(input));
  show();
  fireEvent.change(screen.getByLabelText('Product details'), {
    target: { value: 'Unsent product details' },
  });
  fireEvent.click(
    screen.getByRole('button', { name: 'Save product and start' })
  );
  expect(
    await screen.findByText(/Your edits are still here/)
  ).toBeInTheDocument();
  expect(screen.getByLabelText('Product details')).toHaveValue(
    'Unsent product details'
  );
  fireEvent.click(
    screen.getByRole('button', { name: 'Retry the same action' })
  );
  await waitFor(() =>
    expect(gateway.createMarketingProject).toHaveBeenCalledTimes(2)
  );
  expect(gateway.createMarketingProject.mock.calls[1][0]).toEqual(
    gateway.createMarketingProject.mock.calls[0][0]
  );
});

test('initial configuration outage uses the same readable connection message', async () => {
  gateway.getMarketingCapabilities.mockResolvedValue({
    kind: 'problem',
    problem: { code: 'domain_unavailable', title: 'domain unavailable' },
  });
  show();
  expect(
    await screen.findByText(/We could not connect to the service/)
  ).toBeInTheDocument();
  expect(
    screen.queryByText('Error: marketing-service-unavailable')
  ).not.toBeInTheDocument();
  expect(gateway.createMarketingProject).not.toHaveBeenCalled();
});

describe('a Workspace whose access is still syncing (#309)', () => {
  const syncing = {
    kind: 'problem',
    status: 503,
    problem: {
      code: 'projection_sync_unavailable',
      title: 'Cannot verify workspace existence — upstream unavailable',
      status: 503,
      retryable: true,
    },
  };
  // What `WorkspaceLayout` gives every page.
  const showRetrying = () =>
    render(
      <SWRConfig
        value={{
          provider: () => new Map(),
          dedupingInterval: 0,
          revalidateOnFocus: false,
          onErrorRetry: retryTransientProblems,
        }}
      >
        <MarketingWorkspace />
      </SWRConfig>
    );
  const capabilityReads = () =>
    gateway.getMarketingCapabilities.mock.calls.length;
  const advance = (ms: number) => act(() => jest.advanceTimersByTimeAsync(ms));
  const syncingCopy = messages['error-projection_sync_unavailable'];

  beforeEach(() => jest.useFakeTimers());
  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
    mockMessages = messages;
  });

  test('reads as copy in both languages, never as its code', async () => {
    gateway.getMarketingCapabilities.mockResolvedValue(syncing);
    for (const locale of [messages, zhMessages]) {
      mockMessages = locale;
      const view = showRetrying();
      expect(
        await screen.findByText(locale['error-projection_sync_unavailable'])
      ).toBeInTheDocument();
      expect(document.body).not.toHaveTextContent(
        'projection_sync_unavailable'
      );
      view.unmount();
    }
  });

  test('retries every 5 seconds and clears once a read answers', async () => {
    gateway.getMarketingCapabilities.mockResolvedValueOnce(syncing);
    showRetrying();
    expect(await screen.findByText(syncingCopy)).toBeInTheDocument();
    expect(capabilityReads()).toBe(1);
    await advance(4_000);
    expect(capabilityReads()).toBe(1);
    await advance(1_500);
    expect(capabilityReads()).toBe(2);
    expect(screen.queryByText(syncingCopy)).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  test("other read failures keep SWR's backoff", async () => {
    // SWR's first backoff is 5 s × 2 × (0.5–1.5), rounded down to 5 or 10 s.
    jest.spyOn(Math, 'random').mockReturnValue(0.5);
    gateway.getMarketingCapabilities.mockResolvedValueOnce({
      kind: 'problem',
      status: 503,
      problem: { code: 'service_unavailable', status: 503, retryable: true },
    });
    showRetrying();
    expect(
      await screen.findByText(messages['error-service_unavailable'])
    ).toBeInTheDocument();
    await advance(5_500);
    expect(capabilityReads()).toBe(1);
    await advance(5_000);
    expect(capabilityReads()).toBe(2);
  });

  test('stops after ten minutes and offers Refresh', async () => {
    gateway.getMarketingCapabilities.mockResolvedValue(syncing);
    showRetrying();
    expect(await screen.findByText(syncingCopy)).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Refresh' })
    ).not.toBeInTheDocument();
    await advance(9 * 60_000);
    expect(
      screen.queryByRole('button', { name: 'Refresh' })
    ).not.toBeInTheDocument();
    await advance(60_000 + 1_000);
    // The first read and 120 retries, 5 seconds apart.
    expect(capabilityReads()).toBe(121);
    const refresh = screen.getByRole('button', { name: 'Refresh' });
    await advance(10 * 60_000);
    expect(capabilityReads()).toBe(121);

    gateway.getMarketingCapabilities.mockResolvedValue(
      ok({ paid: { available: false } })
    );
    fireEvent.click(refresh);
    await advance(0);
    expect(capabilityReads()).toBe(122);
    expect(screen.queryByText(syncingCopy)).not.toBeInTheDocument();
  });
});

test('unavailable AI explains configuration and mock must be explicitly selected', async () => {
  query = { test: 'mp_1' };
  show();
  const draft = await screen.findByRole('button', {
    name: /Draft product understanding/,
  });
  expect(draft).toBeDisabled();
  expect(screen.getByText(/AI planning is unavailable/)).toBeInTheDocument();
  fireEvent.click(screen.getByLabelText('Try a controlled Mock example'));
  expect(draft).toBeEnabled();
  gateway.startMarketingPlanning.mockResolvedValue(ok({ id: 'pc_1' }));
  fireEvent.click(draft);
  await waitFor(() =>
    expect(gateway.startMarketingPlanning).toHaveBeenCalledTimes(1)
  );
  expect(gateway.startMarketingPlanning.mock.calls[0][0]).toMatchObject({
    expectedRevision: 1,
    body: { kind: 'understanding', execution_mode: 'mock' },
  });
});

test('the strategy language follows the interface language; the ad language does not', async () => {
  const bodies: Record<
    string,
    { strategy_language: string; ad_language?: string }
  > = {};
  for (const locale of ['en', 'zh-CN']) {
    mockLocale = locale;
    query = { test: 'mp_1' };
    gateway.startMarketingPlanning.mockClear();
    gateway.startMarketingPlanning.mockResolvedValue(ok({ id: 'pc_1' }));
    const view = show();
    fireEvent.click(
      await screen.findByLabelText('Try a controlled Mock example')
    );
    fireEvent.click(
      screen.getByRole('button', { name: /Draft product understanding/ })
    );
    await waitFor(() =>
      expect(gateway.startMarketingPlanning).toHaveBeenCalledTimes(1)
    );
    bodies[locale] = gateway.startMarketingPlanning.mock.calls[0][0].body;
    view.unmount();
  }
  mockLocale = 'en';
  expect(bodies.en.strategy_language).toBe('en-US');
  expect(bodies['zh-CN'].strategy_language).toBe('zh-CN');
  expect(bodies['zh-CN'].ad_language).toEqual(bodies.en.ad_language);
});

test('fact confirmation is required and inference remains visibly unvalidated', async () => {
  query = { test: 'mp_1' };
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...input,
      revision: 3,
      state: 'understanding_draft',
      understanding: sample.understanding,
    })
  );
  gateway.editMarketingProject.mockResolvedValue(
    ok({
      ...input,
      revision: 4,
      state: 'understanding_confirmed',
      understanding: sample.understanding,
    })
  );
  show();
  const confirm = await screen.findByRole('button', {
    name: 'Confirm understanding and continue',
  });
  expect(confirm).toBeDisabled();
  expect(
    screen.getByText('AI inferences \u00b7 Not validated by delivery')
  ).toBeInTheDocument();
  fireEvent.click(
    screen.getByLabelText('This fact and its evidence are accurate')
  );
  fireEvent.click(confirm);
  await waitFor(() => expect(gateway.editMarketingProject).toHaveBeenCalled());
  expect(gateway.editMarketingProject.mock.calls[0][0]).toMatchObject({
    expectedRevision: 3,
    body: {
      command: 'confirm_understanding',
      understanding: { facts: [{ confirmation: 'confirmed' }] },
    },
  });
});

test('unequal expressions are grouped under angles and combo inference limit is visible', async () => {
  query = { test: 'mp_1' };
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...input,
      state: 'plan_draft',
      understanding: sample.understanding,
      plan: sample.plan,
    })
  );
  show();
  await screen.findByRole('button', {
    name: 'Confirm plan and prepare creatives',
  });
  expect(screen.getAllByLabelText('Expression')).toHaveLength(5);
  expect(screen.getByLabelText('Purchase proposition 1')).toHaveValue(
    '清楚理解产品价值'
  );
  expect(screen.getByLabelText('Purchase proposition 2')).toHaveValue(
    '减少购买顾虑'
  );
  expect(
    screen.getByText(
      'This test compares complete combinations. Performance differences cannot be attributed entirely to the angle.'
    )
  ).toBeInTheDocument();
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
});

test('budget validation remains beside the submitted field with localized guidance', async () => {
  query = { test: 'mp_1' };
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...input,
      state: 'plan_draft',
      understanding: sample.understanding,
      plan: sample.plan,
    })
  );
  gateway.editMarketingProject.mockResolvedValue({
    kind: 'problem',
    problem: {
      code: 'validation_error',
      title: 'validation_error',
      detail: 'Request validation failed',
      field_errors: [
        {
          field: 'body.plan.small_round_budget',
          code: 'small_round_budget_exceeds_media_budget',
          message: '小轮预算不能超过大轮预算',
        },
      ],
    },
  });
  show();
  const budget = await screen.findByLabelText('This test media budget (USD)');
  fireEvent.change(budget, { target: { value: '1001' } });
  fireEvent.click(
    screen.getByRole('button', { name: 'Confirm plan and prepare creatives' })
  );
  const message = await screen.findByText(
    'The small-round budget cannot exceed the overall media budget.'
  );
  expect(budget).toHaveAttribute('aria-invalid', 'true');
  expect(budget).toHaveAccessibleDescription(message.textContent!);
  expect(budget).toHaveValue(1001);
  expect(
    screen.queryByText('Request validation failed')
  ).not.toBeInTheDocument();
});

test('nested field errors are associated with inputs and unmapped paths remain visible', async () => {
  query = { test: 'mp_1' };
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...input,
      state: 'plan_draft',
      understanding: sample.understanding,
      plan: sample.plan,
    })
  );
  gateway.editMarketingProject.mockResolvedValue({
    kind: 'problem',
    problem: {
      code: 'validation_error',
      title: 'validation_error',
      detail: 'Request validation failed',
      field_errors: [
        {
          field: 'body.plan.angles.0.expressions.0.name',
          code: 'missing',
          message: 'Field required',
        },
        {
          field: 'body.plan.rules.threshold',
          code: 'greater_than',
          message: 'Input should be greater than 0',
          params: { gt: 0 },
        },
      ],
    },
  });
  show();
  fireEvent.click(
    await screen.findByRole('button', {
      name: 'Confirm plan and prepare creatives',
    })
  );
  // Each field reads by its code and params, never the backend sentence.
  expect(await screen.findByText('This field is required.')).toBeVisible();
  expect(screen.getAllByLabelText('Expression')[0]).toHaveAccessibleDescription(
    'This field is required.'
  );
  expect(
    await screen.findByText(
      'body.plan.rules.threshold: Enter a number greater than 0.'
    )
  ).toBeVisible();
  expect(
    screen.getByText(
      'Review the highlighted fields and correct the test details.'
    )
  ).toBeVisible();
  expect(screen.queryByText(/Field required/)).not.toBeInTheDocument();
});

test('refusals read by their code and params, never the backend text', async () => {
  gateway.createMarketingProject
    .mockResolvedValueOnce({
      kind: 'problem',
      problem: {
        code: 'source_invalid',
        detail: '请粘贴不超过 16000 字的产品资料。',
        title: 'source_invalid',
        params: { limit: 16000 },
      },
    })
    .mockResolvedValueOnce({
      kind: 'problem',
      problem: {
        code: 'planning_in_progress',
        detail: 'Future server guidance',
        title: 'planning_in_progress',
      },
    });
  show();
  fireEvent.change(screen.getByLabelText('Product details'), {
    target: { value: 'Product input' },
  });
  fireEvent.click(
    screen.getByRole('button', { name: 'Save product and start' })
  );
  expect(
    await screen.findByText(
      'Provide product information of at most 16000 characters.'
    )
  ).toBeVisible();
  expect(
    screen.queryByText('请粘贴不超过 16000 字的产品资料。')
  ).not.toBeInTheDocument();
  fireEvent.click(
    screen.getByRole('button', { name: 'Retry the same action' })
  );
  expect(
    await screen.findByText(
      'AI is working or the result needs reconciliation. Check the task status first.'
    )
  ).toBeVisible();
  expect(screen.queryByText('Future server guidance')).not.toBeInTheDocument();
});

test('sharing an expression creates an explicit new pairing with a separate fit reason', async () => {
  query = { test: 'mp_1' };
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...input,
      state: 'plan_draft',
      understanding: sample.understanding,
      plan: sample.plan,
    })
  );
  gateway.editMarketingProject.mockResolvedValue(
    ok({
      ...input,
      state: 'plan_confirmed',
      understanding: sample.understanding,
      plan: sample.plan,
    })
  );
  show();
  const selector = await screen.findByLabelText(
    `Use another angle’s expression for ${sample.plan.angles[1].proposition}`
  );
  const shared = sample.plan.angles[0].expressions[0];
  fireEvent.change(selector, { target: { value: shared.id } });
  expect(screen.getAllByLabelText('Expression')).toHaveLength(6);
  expect(
    screen.getByText(messages['marketing-comparison-view'], {
      selector: 'summary',
    })
  ).toBeInTheDocument();
  // A shared definition stays consistent without overwriting either angle's rationale.
  fireEvent.change(screen.getAllByLabelText('Expression')[0], {
    target: { value: 'Shared product demonstration' },
  });
  fireEvent.click(
    screen.getByRole('button', { name: 'Confirm plan and prepare creatives' })
  );
  await waitFor(() => expect(gateway.editMarketingProject).toHaveBeenCalled());
  const plan = gateway.editMarketingProject.mock.calls[0][0].body.plan;
  const first = plan.angles[0].expressions[0];
  const second = plan.angles[1].expressions.find(
    (e: { id: string }) => e.id === shared.id
  );
  expect(first.name).toBe('Shared product demonstration');
  expect(second.name).toBe(first.name);
  expect(second.fit_reason).not.toBe(first.fit_reason);
  expect(
    plan.combinations.filter(
      (c: { expression_id: string }) => c.expression_id === shared.id
    )
  ).toHaveLength(2);
  expect(plan.groups[1].combination_ids).toHaveLength(4);
});

test('lost AI response reuses the same command intent', async () => {
  query = { test: 'mp_1' };
  gateway.startMarketingPlanning
    .mockRejectedValueOnce(new Error('网络中断'))
    .mockResolvedValueOnce(ok({ id: 'pc_1' }));
  show();
  await screen.findByRole('button', { name: /Draft product understanding/ });
  fireEvent.click(screen.getByLabelText('Try a controlled Mock example'));
  fireEvent.click(
    screen.getByRole('button', { name: /Draft product understanding/ })
  );
  fireEvent.click(
    await screen.findByRole('button', { name: 'Retry the same action' })
  );
  await waitFor(() =>
    expect(gateway.startMarketingPlanning).toHaveBeenCalledTimes(2)
  );
  expect(gateway.startMarketingPlanning.mock.calls[0][0].actionInstanceId).toBe(
    gateway.startMarketingPlanning.mock.calls[1][0].actionInstanceId
  );
});

test('editing copy and narrowing candidates preserves groups and declares limited coverage', async () => {
  query = { test: 'mp_1' };
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...input,
      state: 'plan_draft',
      understanding: sample.understanding,
      plan: sample.plan,
    })
  );
  gateway.editMarketingProject.mockResolvedValue(
    ok({
      ...input,
      state: 'plan_confirmed',
      understanding: sample.understanding,
      plan: sample.plan,
    })
  );
  show();
  await screen.findByRole('button', {
    name: 'Confirm plan and prepare creatives',
  });
  fireEvent.change(screen.getAllByLabelText('Ad copy')[0], {
    target: { value: 'Check vehicle authorization before use.' },
  });
  fireEvent.click(
    screen.getAllByRole('button', {
      name: 'Remove expression from this test',
    })[1]
  );
  fireEvent.click(
    screen.getAllByRole('button', {
      name: 'Remove expression from this test',
    })[3]
  );
  fireEvent.click(
    screen.getAllByRole('button', {
      name: 'Remove expression from this test',
    })[2]
  );
  expect(screen.getAllByLabelText('Expression')).toHaveLength(2);
  expect(
    screen.getByText(messages['marketing-limited-expression-coverage-note'])
  ).toBeInTheDocument();
  fireEvent.click(
    screen.getByRole('button', { name: 'Confirm plan and prepare creatives' })
  );
  await waitFor(() => expect(gateway.editMarketingProject).toHaveBeenCalled());
  const plan = gateway.editMarketingProject.mock.calls[0][0].body.plan;
  expect(plan.combinations).toHaveLength(2);
  expect(plan.combinations[0].body_copy).toBe(
    'Check vehicle authorization before use.'
  );
  expect(plan.groups).toHaveLength(2);
  expect(
    plan.groups
      .flatMap((g: { combination_ids: string[] }) => g.combination_ids)
      .sort()
  ).toEqual(plan.combinations.map((c: { id: string }) => c.id).sort());
  expect(plan.limited_expression_coverage).toBe(true);
  expect(plan.limitations).toContain(
    messages['marketing-limited-expression-coverage-note']
  );
});

test('preparing creatives retains the opened draft across the returned project revision', async () => {
  query = { test: 'mp_1' };
  const confirmed = {
    ...input,
    revision: 5,
    state: 'plan_confirmed',
    understanding: sample.understanding,
    plan: sample.plan,
  };
  gateway.getMarketingProject.mockResolvedValue(ok(confirmed));
  gateway.prepareMarketingGeneration.mockResolvedValue(
    ok({
      ...confirmed,
      revision: 6,
      generation_draft_ids: ['gd_prepared'],
    })
  );
  show();
  fireEvent.click(
    await screen.findByRole('button', {
      name: 'Prepare generation drafts · No charge',
    })
  );
  expect(await screen.findByText('gd_prepared')).toBeInTheDocument();
  await waitFor(() =>
    expect(gateway.prepareMarketingGeneration).toHaveBeenCalledTimes(1)
  );
  expect(screen.getByText('gd_prepared')).toBeInTheDocument();
});

test('removing an extra angle prunes its combinations and groups while keeping a comparison', async () => {
  query = { test: 'mp_1' };
  const plan = JSON.parse(JSON.stringify(sample.plan));
  plan.angles.push({
    ...plan.angles[0],
    id: 'extra_angle',
    expressions: [{ ...plan.angles[0].expressions[0], id: 'extra_expression' }],
  });
  plan.combinations.push({
    ...plan.combinations[0],
    id: 'extra_combination',
    angle_id: 'extra_angle',
    expression_id: 'extra_expression',
  });
  plan.groups.push({
    ...plan.groups[0],
    id: 'extra_group',
    combination_ids: ['extra_combination'],
  });
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...input,
      state: 'plan_draft',
      understanding: sample.understanding,
      plan,
    })
  );
  gateway.editMarketingProject.mockResolvedValue(
    ok({
      ...input,
      state: 'plan_confirmed',
      understanding: sample.understanding,
      plan: sample.plan,
    })
  );
  show();
  const buttons = await screen.findAllByRole('button', {
    name: 'Remove this angle from this test',
  });
  fireEvent.click(buttons[2]);
  expect(
    screen
      .getAllByRole('button', { name: 'Remove this angle from this test' })
      .every((button) => button.hasAttribute('disabled'))
  ).toBe(true);
  fireEvent.click(
    screen.getByRole('button', { name: 'Confirm plan and prepare creatives' })
  );
  await waitFor(() => expect(gateway.editMarketingProject).toHaveBeenCalled());
  const saved = gateway.editMarketingProject.mock.calls[0][0].body.plan;
  expect(saved.angles).toEqual(sample.plan.angles);
  expect(saved.combinations).toEqual(sample.plan.combinations);
  // A plan drafted before the brand baseline is saved with the held default.
  expect(saved.groups).toEqual(
    sample.plan.groups.map((group) => ({ ...group, style: '' }))
  );
  expect(saved.style_variation).toBe('held');
});

test('CSRF 401 preserves unsaved product input and offers sign-in before a manual retry', async () => {
  const csrfFetch = jest.fn().mockResolvedValue({
    ok: false,
    status: 401,
    json: async () => ({ error: { message: 'Unauthorized' } }),
  });
  const commandFetch = jest.fn().mockResolvedValue({
    status: 201,
    headers: new Headers(),
    json: async () => input,
  });
  const real = new DefaultDomainGateway({
    csrfTokenManager: new CsrfTokenManager({
      ttlMs: 60_000,
      fetchFn: csrfFetch,
    }),
    intentTracker: new IntentTracker({ storage: window.localStorage }),
    fetchFn: commandFetch,
  });
  gateway.createMarketingProject.mockImplementation((params) =>
    real.createMarketingProject(params)
  );
  show();
  fireEvent.change(screen.getByLabelText('Product details'), {
    target: { value: '北冰洋 韩国测试：尚未保存的产品资料' },
  });
  fireEvent.click(
    screen.getByRole('button', { name: 'Save product and start' })
  );
  expect(
    await screen.findByText(/Sign in again in a new tab/)
  ).toBeInTheDocument();
  expect(screen.getByLabelText('Product details')).toHaveValue(
    '北冰洋 韩国测试：尚未保存的产品资料'
  );
  const login = screen.getByRole('link', { name: 'Sign in again' });
  expect(login).toHaveAttribute('target', '_blank');
  expect(login).toHaveAttribute('rel', 'noopener noreferrer');
  expect(login.getAttribute('href')).toContain('/auth/login?callbackUrl=');
  expect(commandFetch).not.toHaveBeenCalled();
  expect(csrfFetch).toHaveBeenCalledTimes(1);
  expect(
    screen.queryByText('CSRF token fetch failed: 401')
  ).not.toBeInTheDocument();
  csrfFetch.mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ token: 'fresh-token' }),
  });
  fireEvent.click(
    screen.getByRole('button', { name: 'Retry the same action' })
  );
  await waitFor(() => expect(commandFetch).toHaveBeenCalledTimes(1));
  expect(JSON.parse(commandFetch.mock.calls[0][1].body).source_text).toBe(
    '北冰洋 韩国测试：尚未保存的产品资料'
  );
  expect(gateway.startMarketingPlanning).not.toHaveBeenCalled();
});

test('a command 401 also offers sign-in while preserving the current form', async () => {
  gateway.createMarketingProject.mockResolvedValue({
    kind: 'problem',
    status: 401,
    problem: { code: 'unauthenticated', title: 'unauthenticated' },
  });
  show();
  fireEvent.change(screen.getByLabelText('Product details'), {
    target: { value: 'Preserve this product' },
  });
  fireEvent.click(
    screen.getByRole('button', { name: 'Save product and start' })
  );
  expect(
    await screen.findByRole('link', { name: 'Sign in again' })
  ).toBeInTheDocument();
  expect(screen.getByLabelText('Product details')).toHaveValue(
    'Preserve this product'
  );
  expect(gateway.createMarketingProject).toHaveBeenCalledTimes(1);
});

test('legacy language inference is visible and can be confirmed with a manual English brief', async () => {
  query = { test: 'mp_1' };
  const legacy = {
    ...input,
    revision: 4,
    state: 'plan_confirmed',
    understanding: sample.understanding,
    plan: {
      ...sample.plan,
      ad_language: 'en-US',
      language_confirmation_required: true,
    },
  };
  gateway.getMarketingProject.mockResolvedValue(ok(legacy));
  gateway.editMarketingProject.mockResolvedValue(
    ok({
      ...legacy,
      revision: 5,
      state: 'plan_confirmed',
      plan: { ...legacy.plan, language_confirmation_required: false },
    })
  );
  show();
  const language = await screen.findByLabelText('Advertising language');
  expect(language).toHaveValue('en-US');
  expect(
    screen.getByText(/Inferred from the historical market/)
  ).toBeInTheDocument();
  fireEvent.change(language, { target: { value: 'zh-CN' } });
  fireEvent.click(
    screen.getByRole('button', { name: 'Confirm plan and prepare creatives' })
  );
  await waitFor(() =>
    expect(gateway.editMarketingProject).toHaveBeenCalledWith(
      expect.objectContaining({
        body: expect.objectContaining({
          command: 'confirm_plan',
          plan: expect.objectContaining({
            ad_language: 'zh-CN',
            language_confirmation_required: false,
          }),
        }),
      })
    )
  );
  expect(gateway.startMarketingPlanning).not.toHaveBeenCalled();
});

test('an older API remains readable while new paid drafting and generation stay unavailable', async () => {
  query = { test: 'mp_1' };
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...input,
      language_support: undefined,
      revision: 4,
      state: 'plan_confirmed',
      understanding: sample.understanding,
      plan: {
        ...sample.plan,
        ad_language: undefined,
        strategy_language: undefined,
        language_confirmation_required: undefined,
      },
    })
  );
  show();
  await screen.findByText(
    /Advertising language confirmation is unavailable on this server version/
  );
  expect(
    screen.getByRole('button', {
      name: /^Prepare generation drafts/,
    })
  ).toBeDisabled();
  expect(
    screen.queryByLabelText('Advertising language')
  ).not.toBeInTheDocument();
  expect(gateway.startMarketingPlanning).not.toHaveBeenCalled();
  expect(gateway.prepareMarketingGeneration).not.toHaveBeenCalled();
  expect(gateway.getMarketingProject).toHaveBeenCalledWith(
    expect.objectContaining({ includeLanguages: true })
  );
});

const guidingQuestion = {
  id: 'q_offer',
  question: 'Which offer should this round use?',
  options: ['Current list price', 'Launch discount'],
  multiple: false,
  why_needed: 'The offer changes the business goal.',
  selected: [] as string[],
  custom_answer: '',
};
const blockingReview = {
  id: 'r_overlap',
  kind: 'angle_overlap',
  severity: 'blocking',
  target_ids: ['a1', 'a2'],
  finding: 'Both angles promise the same reassurance.',
  suggestion: 'Merge or sharpen one angle.',
  source: 'ai',
  resolved: false,
};

test('blocking checks and unanswered questions keep confirmation closed until handled', async () => {
  query = { test: 'mp_1' };
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...input,
      state: 'plan_draft',
      understanding: sample.understanding,
      plan: {
        ...sample.plan,
        plan_review: [...sample.plan.plan_review, blockingReview],
        guiding_questions: [guidingQuestion],
      },
    })
  );
  gateway.editMarketingProject.mockResolvedValue(
    ok({ ...input, state: 'plan_confirmed', plan: sample.plan })
  );
  show();
  await screen.findByText('AI check before generation');
  expect(
    screen.getByText('Both angles promise the same reassurance.')
  ).toBeInTheDocument();
  expect(
    screen.getByText(/Applies to: 清楚理解产品价值 · 减少购买顾虑/)
  ).toBeInTheDocument();
  const confirm = screen.getByRole('button', {
    name: 'Confirm plan and prepare creatives',
  });
  expect(confirm).toBeDisabled();
  fireEvent.click(screen.getByLabelText('Launch discount'));
  expect(confirm).toBeDisabled();
  fireEvent.click(screen.getAllByLabelText('Handled')[1]);
  expect(confirm).toBeEnabled();
  fireEvent.click(confirm);
  await waitFor(() => expect(gateway.editMarketingProject).toHaveBeenCalled());
  const call = gateway.editMarketingProject.mock.calls[0][0];
  expect(call.includePlanReview).toBe(true);
  expect(call.body.command).toBe('confirm_plan');
  expect(call.body.plan.guiding_questions[0].selected).toEqual([
    'Launch discount',
  ]);
  expect(call.body.plan.plan_review[1].resolved).toBe(true);
  expect(gateway.getMarketingProject).toHaveBeenCalledWith(
    expect.objectContaining({ includePlanReview: true })
  );
});

test('only saved answers can be used for an AI redraft', async () => {
  query = { test: 'mp_1' };
  gateway.getMarketingCapabilities.mockResolvedValue(
    ok({
      paid: {
        provider: 'gemini',
        model: 'planner',
        available: true,
        maximum_cost: '0.10',
        quote_hash: 'quote',
      },
    })
  );
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...input,
      state: 'plan_draft',
      understanding: sample.understanding,
      plan: {
        ...sample.plan,
        guiding_questions: [
          { ...guidingQuestion, selected: ['Launch discount'] },
        ],
      },
    })
  );
  gateway.startMarketingPlanning.mockResolvedValue(
    ok({ id: 'pc_2', state: 'queued' })
  );
  show();
  const redraft = await screen.findByRole('button', {
    name: /^Ask AI to redraft with these answers/,
  });
  await waitFor(() => expect(redraft).toBeEnabled());
  fireEvent.click(screen.getByLabelText('Current list price'));
  expect(redraft).toBeDisabled();
  expect(
    screen.getByText(
      'Save your answers first. AI redrafts from the saved answers.'
    )
  ).toBeInTheDocument();
  fireEvent.click(screen.getByLabelText('Launch discount'));
  expect(redraft).toBeEnabled();
  fireEvent.click(redraft);
  await waitFor(() =>
    expect(gateway.startMarketingPlanning).toHaveBeenCalledWith(
      expect.objectContaining({
        body: expect.objectContaining({ kind: 'plan', execution_mode: 'byok' }),
      })
    )
  );
});

test('expression cards edit the three elements and show which reworded expressions are blocked', async () => {
  query = { test: 'mp_1' };
  const [first, second] = sample.plan.angles[0].expressions;
  const plan = {
    ...sample.plan,
    angles: [
      {
        ...sample.plan.angles[0],
        expressions: [
          { ...first, name: '高功率与认证保障' },
          {
            ...second,
            name: '高效快充，安全耐用',
            visual_mechanism: first.visual_mechanism,
          },
        ],
      },
      sample.plan.angles[1],
    ],
    plan_review: [
      ...sample.plan.plan_review,
      {
        id: 'system-expression-duplicate-1',
        kind: 'expression_duplicate',
        severity: 'blocking',
        target_ids: ['a1', first.id, second.id],
        finding:
          '角度「清楚理解产品价值」下 「高功率与认证保障」、「高效快充，安全耐用」 的画面机制相同（live_connection_demo）。',
        suggestion: '为其中一个表达补充不同的讲法与画面机制。',
        source: 'system',
        resolved: false,
      },
    ],
  };
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...input,
      state: 'plan_draft',
      understanding: sample.understanding,
      plan,
    })
  );
  const message = 'same mechanism';
  gateway.editMarketingProject.mockResolvedValue({
    kind: 'problem',
    problem: {
      code: 'expressions_not_distinct',
      title: 'expressions_not_distinct',
      detail: message,
      field_errors: [0, 1].map((index) => ({
        field: `body.plan.angles.0.expressions.${index}.visual_mechanism`,
        code: 'expressions_not_distinct',
        message,
      })),
    },
  });
  show();
  await screen.findByText(/的画面机制相同/);
  expect(
    screen.getAllByText(
      'Same visual mechanism as another expression under this angle.'
    )
  ).toHaveLength(2);
  expect(
    screen.getByText(
      'Deterministic check. It clears when these expressions use different visual mechanisms.'
    )
  ).toBeInTheDocument();
  expect(screen.getAllByLabelText('How to tell')[0]).toHaveValue(
    first.how_to_tell
  );
  const mechanisms = screen.getAllByLabelText('Visual mechanism');
  expect(mechanisms[1]).toHaveValue('live_connection_demo');
  // The deterministic finding is recomputed on confirmation, so it does not disable it.
  fireEvent.click(
    screen.getByRole('button', { name: 'Confirm plan and prepare creatives' })
  );
  await waitFor(() =>
    expect(mechanisms[0]).toHaveAttribute('aria-invalid', 'true')
  );
  expect(mechanisms[1]).toHaveAttribute('aria-invalid', 'true');
  fireEvent.change(mechanisms[1], { target: { value: 'other:' } });
  fireEvent.change(screen.getByLabelText('Describe the new visual mechanism'), {
    target: { value: 'Before and after cable test' },
  });
  fireEvent.click(screen.getAllByRole('checkbox', { name: /Alpha/ })[1]);
  fireEvent.click(
    screen.getByRole('button', { name: 'Confirm plan and prepare creatives' })
  );
  await waitFor(() =>
    expect(gateway.editMarketingProject).toHaveBeenCalledTimes(2)
  );
  const sent =
    gateway.editMarketingProject.mock.calls[1][0].body.plan.angles[0]
      .expressions[1];
  expect(sent.visual_mechanism).toBe('other:Before and after cable test');
  expect(sent.evidence_basis.fact_ids).toEqual([]);
});

const systemCheck = (
  kind: string,
  severity: 'blocking' | 'advisory',
  target_ids: string[]
) => ({
  id: `system-${kind}`,
  kind,
  severity,
  target_ids,
  finding: `${kind} finding`,
  suggestion: `${kind} suggestion`,
  source: 'system',
  resolved: false,
});

test('each deterministic check says what clears it (#297)', async () => {
  query = { test: 'mp_1' };
  const plan = {
    ...sample.plan,
    plan_review: [
      ...sample.plan.plan_review,
      systemCheck('expression_duplicate', 'blocking', ['a1']),
      systemCheck('evidence_gap', 'advisory', ['a2']),
      systemCheck('budget_insufficient', 'advisory', ['g1', 'g2']),
      systemCheck('image_brief_stale', 'blocking', ['c1_1', 'e1_1']),
    ],
  };
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...input,
      state: 'plan_draft',
      understanding: sample.understanding,
      plan,
    })
  );
  show();
  const footer = async (kind: string) =>
    within(
      (await screen.findByText(`${kind} finding`)).closest('li')!
    ).getByText(/^Deterministic check\./).textContent;
  expect(await footer('expression_duplicate')).toBe(
    'Deterministic check. It clears when these expressions use different visual mechanisms.'
  );
  expect(await footer('evidence_gap')).toBe(
    'Deterministic check. It clears once the evidence is supplied or verified and these expressions no longer state a gap.'
  );
  expect(await footer('budget_insufficient')).toBe(
    'Deterministic check. It clears when the small-round budget or the number of test groups is adjusted.'
  );
  expect(await footer('image_brief_stale')).toBe(
    "Deterministic check. It clears when this combination's English image brief is rewritten for the current expression."
  );
  // Only an expression duplicate clears by changing the visual mechanism, in either language.
  for (const kind of [
    'evidence_gap',
    'budget_insufficient',
    'image_brief_stale',
  ]) {
    const key = `marketing-review-system-${kind}`;
    expect((messages as Record<string, string>)[key]).not.toMatch(
      /visual mechanism/
    );
    expect((zhMessages as Record<string, string>)[key]).not.toMatch(/视觉机制/);
  }
  expect(
    (zhMessages as Record<string, string>)[
      'marketing-review-system-evidence_gap'
    ]
  ).toMatch(/补充或核对证据/);
  expect(
    (zhMessages as Record<string, string>)[
      'marketing-review-system-budget_insufficient'
    ]
  ).toMatch(/调整小轮预算或测试组数/);
  expect(
    (zhMessages as Record<string, string>)[
      'marketing-review-system-image_brief_stale'
    ]
  ).toMatch(/改写该组合的英文画面要求/);
});

test('a stale English image brief opens flagged in its editor and is rewritten there (#292)', async () => {
  query = { test: 'mp_1' };
  const plan = {
    ...sample.plan,
    plan_review: [
      ...sample.plan.plan_review,
      systemCheck('image_brief_stale', 'blocking', ['c1_1', 'e1_1']),
    ],
  };
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...input,
      state: 'plan_draft',
      understanding: sample.understanding,
      plan,
    })
  );
  gateway.editMarketingProject
    .mockResolvedValueOnce({
      kind: 'problem',
      problem: {
        code: 'image_brief_stale',
        title: 'image_brief_stale',
        detail: 'stale brief',
        field_errors: [
          {
            field: 'body.plan.combinations.0.image_brief.expression',
            code: 'image_brief_stale',
            message: 'stale brief',
          },
        ],
      },
    })
    .mockResolvedValueOnce(
      ok({
        ...input,
        state: 'plan_draft',
        understanding: sample.understanding,
        plan,
      })
    );
  show();
  expect(
    await screen.findByText('English image brief is out of date')
  ).toBeInTheDocument();
  const flag = screen.getByText(
    'Written for an earlier version of this expression. Rewrite it so the images and the Meta ad name follow the current expression.'
  );
  const editor = flag.closest('details')!;
  expect(editor).toHaveAttribute('open');
  expect(editor.parentElement!.closest('details')).toHaveAttribute('open');
  const expression = within(editor).getByLabelText('Expression in English');
  expect(expression).toHaveValue(
    sample.plan.combinations[0].image_brief!.expression
  );

  // The server refuses confirmation; its field error lands beside the brief.
  fireEvent.click(
    screen.getByRole('button', { name: 'Confirm plan and prepare creatives' })
  );
  const refusal =
    'An English image brief was written for an earlier version of its expression. Rewrite that brief for the current expression before confirming.';
  expect(await within(editor).findByText(refusal)).toBeInTheDocument();
  expect(expression).toHaveAttribute('aria-invalid', 'true');

  fireEvent.change(expression, {
    target: { value: 'High power and certified safety' },
  });
  expect(within(editor).queryByText(refusal)).not.toBeInTheDocument();
  fireEvent.click(
    screen.getByRole('button', { name: 'Confirm plan and prepare creatives' })
  );
  await waitFor(() =>
    expect(gateway.editMarketingProject).toHaveBeenCalledTimes(2)
  );
  const sent = gateway.editMarketingProject.mock.calls[1][0].body;
  expect(sent.command).toBe('confirm_plan');
  expect(sent.plan.combinations[0].image_brief.expression).toBe(
    'High power and certified safety'
  );
});

test('a legacy plan opens with pending telling and becomes editable to complete it', async () => {
  query = { test: 'mp_1' };
  const legacy = {
    ...sample.plan,
    plan_review: undefined,
    guiding_questions: undefined,
    angles: sample.plan.angles.map((angle) => ({
      ...angle,
      expressions: angle.expressions.map(
        ({ id, name, description, fit_reason, required_evidence }) => ({
          id,
          name,
          description,
          fit_reason,
          required_evidence,
        })
      ),
    })),
  };
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...input,
      revision: 4,
      state: 'plan_confirmed',
      understanding: sample.understanding,
      plan: legacy,
    })
  );
  gateway.editMarketingProject.mockResolvedValue(
    ok({ ...input, state: 'plan_draft', plan: legacy })
  );
  show();
  const confirm = await screen.findByRole('button', {
    name: 'Confirm plan and prepare creatives',
  });
  expect(screen.getAllByText(/^Way of telling pending/)).toHaveLength(5);
  expect(
    screen.queryByText('AI check before generation')
  ).not.toBeInTheDocument();
  fireEvent.click(confirm);
  await waitFor(() => expect(gateway.editMarketingProject).toHaveBeenCalled());
  const sent = gateway.editMarketingProject.mock.calls[0][0].body.plan;
  expect(sent.plan_review).toEqual([]);
  expect(sent.angles[0].expressions[0]).toMatchObject({
    how_to_tell: '',
    visual_mechanism: '',
    evidence_basis: { fact_ids: [], gap: '' },
  });
});

test('a default draft previews six combinations × two executions and lets the plan set the count', async () => {
  query = { test: 'mp_1' };
  const plan = {
    ...sample.plan,
    executions_per_combination: 2,
    combinations: [
      ...sample.plan.combinations,
      { ...sample.plan.combinations[0], id: 'c1_3' },
    ],
    groups: sample.plan.groups.map((g) =>
      g.id === 'g1'
        ? { ...g, combination_ids: [...g.combination_ids, 'c1_3'] }
        : g
    ),
  };
  const confirmed = {
    ...input,
    revision: 5,
    state: 'plan_confirmed',
    understanding: sample.understanding,
    plan,
  };
  gateway.getMarketingProject.mockResolvedValue(ok(confirmed));
  gateway.prepareMarketingGeneration.mockResolvedValue(
    ok({ ...confirmed, revision: 6, generation_draft_ids: ['gd_prepared'] })
  );
  show();
  expect(
    await screen.findByText(
      'Cost previews cover 6 combinations × 2 executions (12 images).'
    )
  ).toBeInTheDocument();
  expect(gateway.getMarketingProject).toHaveBeenCalledWith(
    expect.objectContaining({ includeExecutionPlan: true })
  );
  fireEvent.click(
    await screen.findByRole('button', {
      name: 'Prepare generation drafts · No charge',
    })
  );
  await waitFor(() =>
    expect(gateway.prepareMarketingGeneration).toHaveBeenCalledTimes(1)
  );
  const call = gateway.prepareMarketingGeneration.mock.calls[0][0];
  expect(call.includeExecutionPlan).toBe(true);
  expect(call.body).not.toHaveProperty('variants');
});

test('a legacy plan without the count previews one execution; overrides are summed', async () => {
  query = { test: 'mp_1' };
  const legacy = {
    ...input,
    revision: 5,
    state: 'plan_confirmed',
    understanding: sample.understanding,
    plan: sample.plan,
  };
  expect(sample.plan).not.toHaveProperty('executions_per_combination');
  gateway.getMarketingProject.mockResolvedValue(ok(legacy));
  const view = show();
  expect(
    await screen.findByText(
      'Cost previews cover 5 combinations × 1 execution (5 images).'
    )
  ).toBeInTheDocument();
  view.unmount();
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...legacy,
      plan: {
        ...sample.plan,
        executions_per_combination: 2,
        combinations: sample.plan.combinations.map((c, i) =>
          i === 0 ? { ...c, executions: 4 } : c
        ),
      },
    })
  );
  show();
  expect(
    await screen.findByText(
      'Cost previews cover 5 combinations with 12 executions in total; some combinations plan their own number.'
    )
  ).toBeInTheDocument();
});

test('the plan editor saves the chosen executions per combination', async () => {
  query = { test: 'mp_1' };
  const draft = {
    ...input,
    state: 'plan_draft',
    understanding: sample.understanding,
    plan: { ...sample.plan, executions_per_combination: 2 },
  };
  gateway.getMarketingProject.mockResolvedValue(ok(draft));
  gateway.editMarketingProject.mockResolvedValue(ok(draft));
  show();
  const select = await screen.findByLabelText('Executions per combination');
  expect(select).toHaveValue('2');
  expect(select).toHaveAccessibleDescription(
    messages['marketing-executions-per-combination-hint']
  );
  fireEvent.change(select, { target: { value: '1' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
  await waitFor(() => expect(gateway.editMarketingProject).toHaveBeenCalled());
  const call = gateway.editMarketingProject.mock.calls[0][0];
  expect(call.body.plan.executions_per_combination).toBe(1);
  expect(call.includeExecutionPlan).toBe(true);
});

test('a draft refused for its style fields is fixed in one step and confirms (#354)', async () => {
  query = { test: 'mp_1' };
  // A live draft: style held, but reworded in the held list and written per group.
  const drafted = {
    ...sample.plan,
    style_variation: 'held',
    style_notes: 'Clean daylight product photography',
    held_constant: [...sample.plan.held_constant, '风格保持不变'],
    groups: sample.plan.groups.map((group) => ({ ...group, style: '保持' })),
  };
  gateway.getMarketingProject.mockResolvedValue(
    ok({
      ...input,
      state: 'plan_draft',
      understanding: sample.understanding,
      plan: drafted,
    })
  );
  gateway.editMarketingProject
    .mockResolvedValueOnce({
      kind: 'problem',
      problem: {
        code: 'style_not_held',
        title: 'style_not_held',
        detail: 'style not held',
        field_errors: [
          {
            field: 'body.plan.held_constant',
            code: 'style_not_held',
            message: 'style not held',
          },
        ],
      },
    })
    .mockResolvedValueOnce(
      ok({
        ...input,
        state: 'plan_confirmed',
        understanding: sample.understanding,
        plan: drafted,
      })
    );
  show();
  fireEvent.click(
    await screen.findByRole('button', {
      name: 'Confirm plan and prepare creatives',
    })
  );
  const fix = await screen.findByRole('button', {
    name: 'Fix for the current style choice',
  });
  expect(
    screen.getByText(`${sample.plan.groups[0].name}: 保持`)
  ).toBeInTheDocument();
  fireEvent.click(fix);
  // The edit clears the refusal, as any edit to the submitted plan does.
  expect(fix).not.toBeInTheDocument();
  fireEvent.click(
    screen.getByRole('button', { name: 'Confirm plan and prepare creatives' })
  );
  await waitFor(() =>
    expect(gateway.editMarketingProject).toHaveBeenCalledTimes(2)
  );
  const sent = gateway.editMarketingProject.mock.calls[1][0].body;
  expect(sent.command).toBe('confirm_plan');
  // The label follows the plan's strategy language (zh-CN here).
  expect(sent.plan.held_constant).toEqual([
    ...sample.plan.held_constant,
    '风格保持不变',
    '视觉风格',
  ]);
  expect(sent.plan.changed).toEqual(sample.plan.changed);
  expect(
    sent.plan.groups.map((group: { style: string }) => group.style)
  ).toEqual(sample.plan.groups.map(() => ''));
});
