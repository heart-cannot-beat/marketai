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
import { errorMessage } from '@/lib/i18n/domain-errors';
import sample from '../../fixtures/marketing-plan.json';
import workflowFixture from '../../fixtures/marketing-workflow.json';
import qcFixture from '../../fixtures/marketing-ai-qc.json';
import boardFixture from '../../fixtures/marketing-board.json';
import { roundRead, storedPlan } from '../../helpers/marketing-round-read';
import messages from '../../../locales/en/common.json';
import zhMessages from '../../../locales/zh-CN/common.json';

/**
 * J-32: the test page's fixed top area keeps the business goal, the round's
 * question, the current gap and one main action; every other action is
 * under "More", and no internal id reaches the visible text.
 */
// #310 checks the old-plan focus in both locales.
let catalog: Record<string, string> = messages;
const translate = (key: string, options?: Record<string, unknown>) =>
  (
    catalog[key] ??
    (options?.defaultValue as string | undefined) ??
    key
  ).replace(/\{\{(\w+)\}\}/g, (_: string, name: string) =>
    String(options?.[name] ?? '')
  );
jest.mock('next-i18next', () => ({
  useTranslation: () => ({ t: translate }),
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
  getMarketingRound: jest.fn(),
  createMarketingProject: jest.fn(),
  editMarketingProject: jest.fn(),
  startMarketingPlanning: jest.fn(),
  prepareMarketingGeneration: jest.fn(),
  listMarketingBrandKits: jest.fn(),
  listProducts: jest.fn(),
  getMarketingAIQCQuote: jest.fn(),
  createGenerationAssetVersionDownloadIntent: jest.fn(),
};
jest.mock('next/router', () => ({
  useRouter: () => ({ query, replace, pathname: '/w/[slug]/experiments' }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws_alpha', slug: 'alpha' } }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => ({
    downloadBlob: jest.fn(async () => new Blob(['image'])),
  }),
}));
jest.mock('@/components/products/ProductThumbnail', () => ({
  ProductThumbnail: () => null,
}));
jest.mock('@/components/library/references/ReferenceThumbnail', () => ({
  ReferenceThumbnail: ({ alt }: { alt: string }) => <span>{alt}</span>,
}));
jest.mock('@/components/workspace/GenerationComposer', () => ({
  GenerationComposer: () => <div data-testid="embedded-composer" />,
}));

type Doc = Record<string, unknown>;
const ok = (data: unknown) => ({ kind: 'ok', data });
const base: Doc = {
  id: 'mp_1',
  language_support: 'v1',
  plan_review_support: 'v1',
  brand_style_support: 'v1',
  revision: 3,
  state: 'input',
  source_text: 'Alpha charger\n65W, three ports.',
  product_id: null,
  understanding: null,
  plan: null,
  latest_call: null,
  evidence_status: 'unvalidated',
  generation_draft_ids: [],
};
const confirmedFacts = {
  ...sample.understanding,
  facts: sample.understanding.facts.map((f) => ({
    ...f,
    confirmation: 'confirmed',
  })),
};
const planned = {
  ...base,
  state: 'plan_confirmed',
  understanding: confirmedFacts,
  plan: sample.plan,
};
const qcRound = qcFixture.row;
// #229: the single-image check is next while the paired check also has a quote.
const singleCheckRound = JSON.parse(
  JSON.stringify(qcFixture.row).replace(
    /"next_action":"review"/g,
    '"next_action":"single_qc"'
  )
);
const boardRound = { ...qcFixture.row, board: boardFixture.screen };
const STATES: [string, Doc, unknown][] = [
  ['product information saved', base, null],
  [
    'understanding to confirm',
    {
      ...base,
      state: 'understanding_draft',
      understanding: sample.understanding,
    },
    null,
  ],
  [
    'plan to draft',
    {
      ...base,
      state: 'understanding_confirmed',
      understanding: confirmedFacts,
    },
    null,
  ],
  [
    'plan to confirm',
    {
      ...base,
      state: 'plan_draft',
      understanding: confirmedFacts,
      plan: sample.plan,
    },
    null,
  ],
  ['round to start', planned, null],
  [
    'drafts prepared',
    { ...planned, generation_draft_ids: ['gd_7f3a9c21b4', 'gd_8e1d0b77c2'] },
    null,
  ],
  ['images to check', planned, qcRound],
  ['single-image check to run', planned, singleCheckRound],
  ['images to generate and review', planned, boardRound],
  ['delivery data to import', planned, workflowFixture.bound],
  ['results to read', planned, workflowFixture.evaluated],
];

function show() {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <MarketingWorkspace />
    </SWRConfig>
  );
}

function folded(element: Element) {
  if (element.closest('[hidden],[aria-hidden="true"]')) return true;
  let node: Element | null = element;
  while (node) {
    const details: HTMLDetailsElement | null = node.closest('details');
    if (!details) return false;
    const summary = details.querySelector(':scope > summary');
    if (!details.open && !summary?.contains(element)) return true;
    node = details.parentElement;
  }
  return false;
}

/** Text a person can read without opening a fold or a dialog. */
function visibleText(root: Element) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const parts: string[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const parent = node.parentElement;
    if (!parent || parent.closest('script,style,[role="dialog"]')) continue;
    if (!folded(parent)) parts.push(node.textContent ?? '');
  }
  return parts.join(' ');
}

/** Filled (main) buttons a person can see on the page, outside dialogs. */
function mainButtons() {
  return Array.from(document.querySelectorAll('button')).filter(
    (button) =>
      button.classList.contains('bg-gray-900') &&
      button.classList.contains('text-white') &&
      !button.closest('[role="dialog"]') &&
      !folded(button)
  );
}

const INTERNAL_ID = /\b[A-Za-z]{1,10}_[A-Za-z0-9]{6,}/;

function workflowOf(doc: Doc, round: unknown, nextAction?: string) {
  return ok({
    project_id: doc.id,
    rounds: round
      ? [
          {
            id: (round as { id: string }).id,
            ordinal: 1,
            state: (round as { state: string }).state,
          },
        ]
      : [],
    current: round,
    next_action: nextAction ?? (round ? 'review_creatives' : 'freeze_round'),
  });
}

async function open(doc: Doc, round: unknown, nextAction?: string) {
  query = { test: String(doc.id) };
  gateway.getMarketingProject.mockResolvedValue(ok(doc));
  gateway.getMarketingWorkflow.mockResolvedValue(
    workflowOf(doc, round, nextAction)
  );
  show();
  const bar = await screen.findByTestId('marketing-focus');
  await waitFor(() =>
    expect(bar).not.toHaveTextContent(catalog['marketing-focus-gap-loading'])
  );
  return bar;
}

beforeEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
  query = {};
  catalog = messages;
  gateway.getMarketingCapabilities.mockResolvedValue(
    ok({
      paid: {
        provider: 'gemini',
        model: 'gemini-planner',
        available: true,
        maximum_cost: '0.05',
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
  gateway.getMarketingAIQCQuote.mockResolvedValue(ok(qcFixture.quote));
  gateway.createGenerationAssetVersionDownloadIntent.mockResolvedValue(
    ok({ download_grant: { url: 'http://localhost/private.png' } })
  );
  URL.createObjectURL = jest.fn(() => 'blob:image');
  URL.revokeObjectURL = jest.fn();
});

test.each(STATES)(
  '%s: the top area keeps goal, question, gap and one main action',
  async (_, doc, round) => {
    const bar = await open(doc, round);
    expect(
      within(bar).getByText(messages['marketing-focus-goal'])
    ).toBeVisible();
    expect(
      within(bar).getByText(messages['marketing-focus-question'])
    ).toBeVisible();
    expect(
      within(bar).getByText(messages['marketing-focus-now'])
    ).toBeVisible();
    // At most one filled button anywhere on the page, whatever the state.
    expect(mainButtons().length).toBeLessThanOrEqual(1);
    // The bar's own action is the main one or a step into its section.
    const actions = within(bar)
      .getAllByRole('button')
      .filter((b) => !b.closest('details'));
    expect(actions.length).toBeLessThanOrEqual(1);
  }
);

test.each(STATES)(
  '%s: no internal id appears in the visible text',
  async (_, doc, round) => {
    await open(doc, round);
    expect(visibleText(document.body)).not.toMatch(INTERNAL_ID);
  }
);

test('every other page action sits under More', async () => {
  const bar = await open(
    {
      ...base,
      state: 'understanding_draft',
      understanding: sample.understanding,
    },
    null
  );
  const more = within(bar).getByText(messages['marketing-focus-more'])
    .parentElement as HTMLDetailsElement;
  expect(more.open).toBe(false);
  for (const label of [
    messages['marketing-new-test'],
    messages['marketing-update-product-information'],
    messages['marketing-save-changes'],
  ])
    expect(within(more).getByRole('button', { name: label })).toBeTruthy();
  // Nothing of these is repeated in the page body.
  expect(
    screen.getAllByRole('button', { name: messages['marketing-save-changes'] })
  ).toHaveLength(1);
  // The facts still need confirming, so the main action says so and waits.
  expect(bar).toHaveTextContent('Product facts still to confirm: 1.');
  expect(
    within(bar).getByRole('button', {
      name: /Confirm understanding and draft the test plan/,
    })
  ).toBeDisabled();
});

test('a product card starts the test in one click with its card carried', async () => {
  window.sessionStorage.setItem('products:alpha:marketing-test', 'prod_1');
  gateway.createMarketingProject.mockResolvedValue(
    ok({ ...base, id: 'mp_new', product_id: 'prod_1', revision: 1 })
  );
  show();
  await waitFor(() =>
    expect(gateway.createMarketingProject).toHaveBeenCalledTimes(1)
  );
  expect(gateway.createMarketingProject.mock.calls[0][0].body).toEqual({
    source_text: '',
    product_id: 'prod_1',
  });
  await waitFor(() => expect(query.test).toBe('mp_new'));
  // The handoff applies once; a refresh does not start a second test.
  expect(
    window.sessionStorage.getItem('products:alpha:marketing-test')
  ).toBeNull();
});

test('confirming the understanding drafts the plan, each with its own command key', async () => {
  const bar = await open(
    { ...base, state: 'understanding_draft', understanding: confirmedFacts },
    null
  );
  gateway.editMarketingProject.mockResolvedValue(
    ok({
      ...base,
      revision: 4,
      state: 'understanding_confirmed',
      understanding: confirmedFacts,
    })
  );
  gateway.startMarketingPlanning.mockResolvedValue(ok({ id: 'pc_1' }));
  fireEvent.click(
    within(bar).getByRole('button', {
      name: /Confirm understanding and draft the test plan/,
    })
  );
  await waitFor(() =>
    expect(gateway.startMarketingPlanning).toHaveBeenCalledTimes(1)
  );
  const edit = gateway.editMarketingProject.mock.calls[0][0];
  const planning = gateway.startMarketingPlanning.mock.calls[0][0];
  expect(edit.body.command).toBe('confirm_understanding');
  expect(planning).toMatchObject({
    expectedRevision: 4,
    body: { kind: 'plan', execution_mode: 'byok', quote_hash: 'quote' },
  });
  expect(planning.actionInstanceId).not.toBe(edit.actionInstanceId);
});

test('confirming the plan prepares the free generation drafts', async () => {
  const bar = await open(
    {
      ...base,
      state: 'plan_draft',
      understanding: confirmedFacts,
      plan: sample.plan,
    },
    null
  );
  gateway.editMarketingProject.mockResolvedValue(
    ok({ ...planned, revision: 4 })
  );
  gateway.prepareMarketingGeneration.mockResolvedValue(
    ok({ ...planned, revision: 4, generation_draft_ids: ['gd_1'] })
  );
  fireEvent.click(
    within(bar).getByRole('button', {
      name: messages['marketing-confirm-plan-and-prepare-creatives'],
    })
  );
  await waitFor(() =>
    expect(gateway.prepareMarketingGeneration).toHaveBeenCalledTimes(1)
  );
  expect(gateway.editMarketingProject.mock.calls[0][0].body.command).toBe(
    'confirm_plan'
  );
  expect(gateway.prepareMarketingGeneration.mock.calls[0][0]).toMatchObject({
    expectedRevision: 4,
    body: { provider: 'mock', model: 'mock-v1' },
  });
});

test('the review step opens the first image that needs it', async () => {
  const bar = await open(planned, boardRound);
  expect(bar).toHaveTextContent(/Images still to generate: \d+\./);
  // Generation comes first; once drafts exist the bar steps into the editor.
  expect(
    within(bar).getByRole('button', {
      name: messages['marketing-prepare-generation-drafts-no-charge'],
    })
  ).toBeInTheDocument();
});

test('a service diagnostic never reaches the page', () => {
  // The page speaks through `error-<code>` (#253); `detail` is for logs.
  expect(
    errorMessage(translate as never, {
      code: 'product_unavailable',
      detail: '冻结的产品版本已不可用。',
    })
  ).toBe(messages['error-product_unavailable']);
  expect(
    errorMessage(translate as never, {
      code: 'source_invalid',
      detail: 'Server guidance',
      params: { limit: 16000 },
    })
  ).not.toContain('Server guidance');
});

/**
 * #310: a plan confirmed before the advertising language (J-02) and the
 * expression details (#222) reads as still to confirm. Once the current round
 * froze it, the page follows that round instead of asking to confirm it again.
 */
const legacy: Doc = {
  ...base,
  // The round froze this revision.
  revision: workflowFixture.evaluated.plan_revision,
  state: 'plan_confirmed',
  understanding: confirmedFacts,
  // The frozen plan as the project read projects it: an inferred language.
  plan: {
    ...workflowFixture.evaluated.snapshot.plan,
    ad_language: 'en-US',
    language_confirmation_required: true,
    strategy_language: 'zh-CN',
  },
};
const closedRound = {
  ...workflowFixture.evaluated,
  state: 'closed',
  evaluation: {
    ...workflowFixture.evaluated.evaluation,
    budget_exhausted: false,
  },
  close: {
    evaluation_id: workflowFixture.evaluated.evaluation.id,
    plan_hash: workflowFixture.evaluated.snapshot.plan_hash,
    execution_mode: 'mock',
    closed_at: '2025-01-20T00:00:00Z',
    reason: 'insufficient_evidence',
    note: 'Simulated delivery only.',
  },
};
const OLD_PLAN_ROUNDS: [string, unknown, string, string][] = [
  [
    'delivery data to import',
    workflowFixture.bound,
    'import_delivery_data',
    'marketing-focus-gap-import',
  ],
  [
    'results to read',
    workflowFixture.evaluated,
    'close_round',
    'marketing-focus-gap-close',
  ],
  [
    'round ended',
    closedRound,
    'prepare_next_plan',
    'marketing-focus-gap-next_plan',
  ],
];
const LOCALES = [
  ['en', messages],
  ['zh-CN', zhMessages],
] as [string, Record<string, string>][];
const confirmPlan = () =>
  screen.queryByRole('button', {
    name: catalog['marketing-confirm-plan-and-prepare-creatives'],
  });

test.each(
  LOCALES.flatMap(([locale, strings]) =>
    OLD_PLAN_ROUNDS.map(
      ([state, round, next, gap]) =>
        [locale, state, strings, round, next, gap] as const
    )
  )
)(
  '%s: an old plan frozen into the current round (%s) follows the round',
  async (_, __, strings, round, next, gap) => {
    catalog = strings;
    const bar = await open(legacy, round, next);
    expect(bar).toHaveTextContent(translate(gap, { count: 5 }));
    expect(bar).not.toHaveTextContent(catalog['marketing-focus-gap-language']);
    expect(bar).not.toHaveTextContent(
      catalog['marketing-focus-gap-plan-ready']
    );
    expect(confirmPlan()).not.toBeInTheDocument();
    expect(mainButtons().length).toBeLessThanOrEqual(1);
    // The bar's action steps into the round's section; it edits no plan.
    for (const action of within(bar)
      .queryAllByRole('button')
      .filter((b) => !b.closest('details')))
      fireEvent.click(action);
    expect(gateway.editMarketingProject).not.toHaveBeenCalled();
  }
);

test('an old plan waits for its round instead of flashing a plan confirmation', async () => {
  query = { test: String(legacy.id) };
  gateway.getMarketingProject.mockResolvedValue(ok(legacy));
  let resolve: (value: unknown) => void = () => undefined;
  gateway.getMarketingWorkflow.mockReturnValue(
    new Promise((settle) => {
      resolve = settle;
    })
  );
  show();
  const bar = await screen.findByTestId('marketing-focus');
  expect(bar).toHaveTextContent(messages['marketing-focus-gap-loading']);
  expect(bar).not.toHaveTextContent(messages['marketing-focus-gap-language']);
  expect(confirmPlan()).not.toBeInTheDocument();
  await act(async () =>
    resolve(workflowOf(legacy, workflowFixture.evaluated, 'close_round'))
  );
  await waitFor(() =>
    expect(bar).toHaveTextContent(messages['marketing-focus-gap-close'])
  );
  expect(confirmPlan()).not.toBeInTheDocument();
});

test.each([
  ['no round yet', legacy, null],
  [
    'a newer revision than the round',
    { ...legacy, revision: Number(legacy.revision) + 1 },
    workflowFixture.evaluated,
  ],
] as [string, Doc, unknown][])(
  'an old plan with %s still asks to confirm its language',
  async (_, doc, round) => {
    const bar = await open(doc, round);
    expect(bar).toHaveTextContent(messages['marketing-focus-gap-language']);
    expect(
      within(bar).getByRole('button', {
        name: messages['marketing-confirm-plan-and-prepare-creatives'],
      })
    ).toBeInTheDocument();
  }
);

test('an old plan whose workflow cannot be read keeps the plan confirmation', async () => {
  query = { test: String(legacy.id) };
  gateway.getMarketingProject.mockResolvedValue(ok(legacy));
  gateway.getMarketingWorkflow.mockRejectedValue(new Error('offline'));
  show();
  const bar = await screen.findByTestId('marketing-focus');
  await waitFor(() =>
    expect(bar).toHaveTextContent(messages['marketing-focus-gap-language'])
  );
  expect(
    within(bar).getByRole('button', {
      name: messages['marketing-confirm-plan-and-prepare-creatives'],
    })
  ).toBeInTheDocument();
});

// The board's first image to change asks for a revision (#310 decision 2).
const reviseRound = JSON.parse(
  JSON.stringify(boardRound).replace(
    /"next_action":"(generate|single_qc|paired_qc|review)"/g,
    '"next_action":"none"'
  )
);
test.each([
  ['generate', 'en', messages, boardRound],
  ['revise', 'en', messages, reviseRound],
  ['generate', 'zh-CN', zhMessages, boardRound],
] as [string, string, Record<string, string>, unknown][])(
  "an old plan's round with images to %s (%s) says it cannot make new images",
  async (step, _, strings, round) => {
    catalog = strings;
    const bar = await open(legacy, round);
    expect(bar).toHaveTextContent(
      catalog['marketing-focus-gap-round-predates-language']
    );
    expect(bar).not.toHaveTextContent(
      translate(`marketing-focus-gap-${step}`, { count: 1 })
    );
    // No action: neither in the bar nor under More.
    expect(
      within(bar)
        .queryAllByRole('button')
        .filter((b) => !b.closest('details'))
    ).toHaveLength(0);
    expect(
      screen.queryByRole('button', {
        name: catalog['marketing-prepare-generation-drafts-no-charge'],
      })
    ).not.toBeInTheDocument();
    expect(mainButtons()).toHaveLength(0);
    expect(gateway.prepareMarketingGeneration).not.toHaveBeenCalled();
    expect(gateway.editMarketingProject).not.toHaveBeenCalled();
  }
);

/**
 * #367: a plan drafted since #222 / #228 and #273, frozen into a round that
 * has ended. The round read returns the frozen plan by the parameters the
 * page sends, and the bar steps only into a section the page shows.
 */
const newPlan = storedPlan(true);
const newPlanProject: Doc = {
  ...base,
  // The round froze this revision.
  revision: closedRound.plan_revision,
  state: 'plan_confirmed',
  understanding: confirmedFacts,
  plan: newPlan,
};
async function openEnded(doc: Doc) {
  query = { test: String(doc.id) };
  gateway.getMarketingProject.mockResolvedValue(ok(doc));
  gateway.getMarketingWorkflow.mockImplementation(async (params) =>
    workflowOf(
      doc,
      {
        ...closedRound,
        snapshot: {
          ...closedRound.snapshot,
          plan: roundRead(newPlan, params),
        },
      },
      // What the server answers whatever the project's plan (#367 triage).
      'prepare_next_plan'
    )
  );
  show();
  const bar = await screen.findByTestId('marketing-focus');
  await waitFor(() =>
    expect(bar).not.toHaveTextContent(catalog['marketing-focus-gap-loading'])
  );
  return bar;
}
function stepInto(bar: HTMLElement, label: string, id: string) {
  fireEvent.click(within(bar).getByRole('button', { name: label }));
  const target = document.getElementById(id);
  expect(target).toBeInTheDocument();
  expect(document.activeElement).toBe(target);
}

test.each(LOCALES)(
  "%s: a new plan's ended round steps into planning the next round (#367)",
  async (_, strings) => {
    catalog = strings;
    const bar = await openEnded(newPlanProject);
    expect(bar).toHaveTextContent(catalog['marketing-focus-gap-next_plan']);
    stepInto(
      bar,
      catalog['marketing-focus-go-next_plan'],
      'marketing-next-plan'
    );
    expect(document.getElementById('marketing-freeze')).not.toBeInTheDocument();
    expect(mainButtons().length).toBeLessThanOrEqual(1);
  }
);

test.each(LOCALES)(
  '%s: a new plan confirmed after the ended round steps into starting its round (#367)',
  async (_, strings) => {
    catalog = strings;
    const bar = await openEnded({
      ...newPlanProject,
      // Planning requested (+1), then the drafted plan confirmed (+1).
      revision: closedRound.plan_revision + 2,
      plan: { ...newPlan, question: 'A newer question' },
    });
    expect(bar).toHaveTextContent(catalog['marketing-focus-gap-freeze']);
    expect(bar).not.toHaveTextContent(catalog['marketing-focus-gap-next_plan']);
    stepInto(bar, catalog['marketing-focus-go-freeze'], 'marketing-freeze');
    expect(
      document.getElementById('marketing-next-plan')
    ).not.toBeInTheDocument();
    expect(mainButtons().map((b) => b.textContent)).toEqual([
      catalog['marketing-freeze-and-review'],
    ]);
  }
);

test.each(LOCALES)(
  '%s: a failed next-plan request keeps the bar on planning the next round (#367)',
  async (_, strings) => {
    catalog = strings;
    const bar = await openEnded({
      ...newPlanProject,
      // Requesting the plan advanced the revision; the failed call left the plan as it was.
      revision: closedRound.plan_revision + 1,
      latest_call: {
        id: 'pc_failed',
        operation_id: 'marketing.plan',
        kind: 'plan',
        state: 'failed',
        execution_mode: 'mock',
        error_code: 'planner_output_invalid',
        maximum_cost: '0',
      },
    });
    // Both sections show; the bar asks to retry the plan (#367 decision 3).
    expect(document.getElementById('marketing-freeze')).toBeInTheDocument();
    expect(bar).toHaveTextContent(catalog['marketing-focus-gap-next_plan']);
    stepInto(
      bar,
      catalog['marketing-focus-go-next_plan'],
      'marketing-next-plan'
    );
    expect(mainButtons().length).toBeLessThanOrEqual(1);
  }
);
