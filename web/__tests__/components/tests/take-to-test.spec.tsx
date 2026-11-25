/**
 * @jest-environment jsdom
 */
import type { ReactNode } from 'react';
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { SWRConfig } from 'swr';
import { TakeToTest } from '@/components/tests/TakeToTest';
import type { TestQuote, TestRound } from '@/lib/tests/take-to-test';
import { ad, ideasDetail, product } from '../ideas/fixtures';

/**
 * "Take to test" (ADR 0019, design §5.11, #256): three answers, the quote's
 * "enough?" on the right, one button "Start the test · at most $X"; after
 * the start, the checks, only the ads with a problem open, then the ad
 * package made by itself and downloaded.
 */
const mockPush = jest.fn();
jest.mock('next/router', () => ({
  useRouter: () => ({
    asPath: '/w/acme/p/prod_home/take-to-test',
    query: {},
    push: mockPush,
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'ws_1', role: 'OWNER' },
  }),
}));
const mockGateway = {
  getMarketingIdeas: jest.fn(),
  listMarketingTests: jest.fn(),
  getMarketingTestQuote: jest.fn(),
  startMarketingTest: jest.fn(),
  getMarketingTest: jest.fn(),
  getMarketingRound: jest.fn(),
  completeMarketingTestStart: jest.fn(),
  cancelMarketingTest: jest.fn(),
  downloadMarketingHandoff: jest.fn(),
  retryMarketingHandoff: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/components/workspace/useMarketingImageSource', () => ({
  useMarketingImageSource: (id: string) => ({
    imageUrl: `blob:image-${id}`,
    imageFailed: false,
    onImageError: jest.fn(),
  }),
}));

function ok<T>(data: T, status = 200) {
  return {
    kind: status === 201 ? ('created' as const) : ('ok' as const),
    status,
    data,
    meta: {},
  };
}

function refused(code: string, params?: Record<string, unknown>) {
  return {
    kind: 'problem' as const,
    status: 409,
    problem: {
      type: 'about:blank',
      title: code,
      status: 409,
      detail: '',
      instance: '',
      code,
      trace_id: 't',
      retryable: false,
      field_errors: [],
      params,
    },
    meta: {},
  };
}

const KEPT = ideasDetail({
  ads: [
    ad('av_1', 'c1', { kept: true, status: 'exportable' }),
    ad('av_3', 'c3', { kept: true, status: 'exportable' }),
    ad('av_2', 'c2'),
  ],
  pending: [],
});

function quote(extra: Partial<TestQuote> = {}): TestQuote {
  return {
    available: true,
    unavailable_reason: null,
    plan_error: null,
    not_exportable: [],
    ideas_revision: 3,
    execution_mode: 'byok',
    comparison: 'claims',
    question:
      'Which of these 2 buying claims brings orders at a cost per order of 30 USD or less?',
    subjects: [
      {
        kind: 'claim',
        angle_id: 'a1',
        expression_id: null,
        title: 'Plug in at night and forget about it',
        combination_ids: ['c1'],
        asset_version_ids: ['av_1'],
        budget: '600.00',
        expected_orders: 20,
      },
      {
        kind: 'claim',
        angle_id: 'a2',
        expression_id: null,
        title: 'Safe outdoors',
        combination_ids: ['c3'],
        asset_version_ids: ['av_3'],
        budget: '600.00',
        expected_orders: 20,
      },
    ],
    target_cost: '30.00',
    round_budget: '1200.00',
    currency: 'USD',
    duration_days: 7,
    min_purchases: 20,
    tier: 'enough',
    needed_budget: '1200.00',
    suggested_subjects: null,
    start_date: '2026-11-26',
    end_date: '2026-12-02',
    conclusion_date: '2026-12-11',
    account_timezone: 'UTC',
    attribution_window: '7d_click_1d_view',
    attribution_lag_days: 8,
    landing_page: 'https://acmecharge.example/home',
    landing_page_source: 'product_link',
    checks: {
      available: true,
      unavailable_reason: null,
      quantity: 2,
      maximum_cost: '0.24',
      currency: 'USD',
    },
    quote_hash: 'a'.repeat(64),
    ...extra,
  } as TestQuote;
}

const TEST = {
  id: 'mp_test',
  revision: 1,
  product_id: 'prod_home',
  source_project_id: 'mp_secret',
  comparison: 'claims',
  plan_summary: {
    target_cost: '30.00',
    round_budget: '1200.00',
    duration_days: 7,
    currency: 'USD',
    landing_page: 'https://acmecharge.example/home',
  },
  rounds: [
    {
      id: 'mr_1',
      ordinal: 1,
      state: 'reviewing',
      start_date: '2026-11-26',
      end_date: '2026-12-02',
      effective_start_date: '2026-11-26',
      effective_end_date: '2026-12-02',
    },
  ],
  current_round_id: 'mr_1',
  created_at: '2026-11-25T08:00:00+00:00',
};

const finding = (checkId: string, status: string, rationale = '') => ({
  check_id: checkId,
  status,
  confidence: 'medium',
  visual_evidence: '',
  rationale,
  suggested_fix: status === 'pass' ? '' : 'Show the enclosure in the rain.',
  fact_ids: [],
  model_status: status,
});

function testRound(
  states: [string, string],
  extra: Partial<TestRound> = {}
): TestRound {
  return {
    id: 'mr_1',
    ordinal: 1,
    state: 'reviewing',
    revision: 4,
    assets: [
      {
        asset_version_id: 'av_1',
        headline: 'Plug in. Go to bed.',
        paired_ai_qc: {
          id: 'qc_1',
          state: states[0],
          result:
            states[0] === 'succeeded'
              ? { findings: [finding('comparison_faithful', 'pass')] }
              : null,
        },
      },
      {
        asset_version_id: 'av_3',
        headline: 'Rain or shine.',
        paired_ai_qc: {
          id: 'qc_3',
          state: states[1],
          result:
            states[1] === 'succeeded'
              ? {
                  findings: [
                    finding(
                      'comparison_faithful',
                      'fail',
                      'It looks like the other claim.'
                    ),
                  ],
                }
              : null,
        },
      },
    ],
    handoff: null,
    ...extra,
  } as unknown as TestRound;
}

const handoff = (state: string) => ({
  id: 'mh_1',
  state,
  error_code: null,
  manifest_hash: 'h',
  quality_current: true,
  entries: [
    {
      entry_id: 'e1',
      asset_version_id: 'av_1',
      headline: 'Plug in. Go to bed.',
      ad_name: 'MA-7Q2 · Plug in. Go to bed.',
    },
    {
      entry_id: 'e3',
      asset_version_id: 'av_3',
      headline: 'Rain or shine.',
      ad_name: 'MA-7Q2 · Rain or shine.',
    },
  ],
});

const envelope = (round: TestRound) => ok({ id: round.id, data: round });

function Fresh({ children }: { children: ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

function renderScreen() {
  return render(
    <Fresh>
      <TakeToTest product={product()} />
    </Fresh>
  );
}

async function answer() {
  fireEvent.change(
    await screen.findByLabelText('Up to what cost per order is it a success?'),
    { target: { value: '30' } }
  );
  fireEvent.change(screen.getByLabelText('How much will this round spend?'), {
    target: { value: '1,200' },
  });
}

const startButton = () => screen.getByTestId('take-start');

beforeEach(() => {
  jest.clearAllMocks();
  mockGateway.getMarketingIdeas.mockResolvedValue(ok(KEPT));
  mockGateway.listMarketingTests.mockResolvedValue(
    ok({ product_id: 'prod_home', tests: [], earlier_tests: [] })
  );
  mockGateway.getMarketingTestQuote.mockResolvedValue(ok(quote()));
  mockGateway.getMarketingTest.mockResolvedValue(ok(TEST));
});

describe('TakeToTest: the three questions and the quote (#256)', () => {
  it('asks three questions; the button then carries the quote’s maximum', async () => {
    renderScreen();
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Test these 2 ads' })
    ).toBeInTheDocument();
    expect(startButton()).toBeDisabled();
    expect(mockGateway.getMarketingTestQuote).not.toHaveBeenCalled();
    expect(
      screen.getByText(/Answer the three questions, and this side tells you/)
    ).toBeInTheDocument();
    // How long: 7 days is chosen to begin with.
    expect(screen.getByRole('button', { name: '7 days' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );

    await answer();
    await waitFor(() =>
      expect(startButton()).toHaveTextContent('Start the test · at most $0.24')
    );
    expect(startButton()).toBeEnabled();
    expect(mockGateway.getMarketingTestQuote).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      projectId: 'mp_secret',
      query: expect.objectContaining({
        target_cost: '30',
        round_budget: '1200',
        duration_days: '7',
      }),
    });
    const panel = screen.getByTestId('test-quote');
    expect(panel).toHaveTextContent('Enough for a verdict');
    expect(panel).toHaveTextContent(
      'Each claim gets about 20 orders, if each order costs exactly your target.'
    );
    expect(panel).toHaveTextContent('Result expected on December 11, 2026.');
    expect(panel).not.toHaveTextContent('A verdict on all of them needs');
    // The round's question is the quote's sentence, with what it compares.
    const question = screen.getByTestId('test-question');
    expect(question).toHaveTextContent(
      'Which of these 2 buying claims brings orders'
    );
    expect(question).toHaveTextContent('Safe outdoors');
  });

  it('short of money: only a rough read, what a verdict needs and a suggestion', async () => {
    mockGateway.getMarketingTestQuote.mockResolvedValue(
      ok(
        quote({
          tier: 'rough',
          needed_budget: '1200.00',
          suggested_subjects: 1,
          subjects: quote().subjects.map((s) => ({
            ...s,
            budget: '150.00',
            expected_orders: 5,
          })),
        })
      )
    );
    renderScreen();
    await answer();
    const panel = await screen.findByTestId('test-verdict');
    expect(panel).toHaveTextContent('Only a rough read');
    expect(panel).toHaveTextContent('Each claim gets about 5 orders');
    expect(screen.getByTestId('test-needed')).toHaveTextContent(
      'A verdict on all of them needs $1,200 this round.'
    );
    expect(panel).toHaveTextContent(
      'Or compare fewer: with 1 of them, each would get enough.'
    );
  });

  it('asks for the landing page only when the product has no link', async () => {
    mockGateway.getMarketingTestQuote.mockResolvedValue(
      ok(
        quote({
          available: false,
          unavailable_reason: 'landing_page_required',
          landing_page: null,
          landing_page_source: null,
        })
      )
    );
    renderScreen();
    await answer();
    const landing = await screen.findByLabelText(
      'Which web address do the ads send people to?'
    );
    expect(startButton()).toBeDisabled();

    mockGateway.getMarketingTestQuote.mockResolvedValue(
      ok(
        quote({
          landing_page: 'https://acmecharge.example/home',
          landing_page_source: 'answer',
        })
      )
    );
    fireEvent.change(landing, {
      target: { value: 'https://acmecharge.example/home' },
    });
    await waitFor(() => expect(startButton()).toBeEnabled());
    expect(mockGateway.getMarketingTestQuote).toHaveBeenLastCalledWith(
      expect.objectContaining({
        query: expect.objectContaining({
          landing_page: 'https://acmecharge.example/home',
        }),
      })
    );
  });

  it('keeps the defaults folded, each changeable, the currency among them', async () => {
    renderScreen();
    await answer();
    const defaults = await screen.findByTestId('take-defaults');
    expect(defaults).not.toHaveAttribute('open');
    fireEvent.change(within(defaults).getByLabelText('Currency'), {
      target: { value: 'EUR' },
    });
    await waitFor(() =>
      expect(mockGateway.getMarketingTestQuote).toHaveBeenLastCalledWith(
        expect.objectContaining({
          query: expect.objectContaining({ currency: 'EUR' }),
        })
      )
    );
  });

  it('sends a kept ad that cannot be exported yet to the check before download', async () => {
    mockGateway.getMarketingTestQuote.mockResolvedValue(
      ok(
        quote({
          available: false,
          unavailable_reason: 'ads_not_exportable',
          not_exportable: [
            { asset_version_id: 'av_3', status: 'draft', reasons: [] },
          ],
        })
      )
    );
    renderScreen();
    await answer();
    const note = await screen.findByTestId('test-unavailable');
    expect(note).toHaveTextContent(
      'One of the kept ads can’t go into an ad package yet.'.replace('’', "'")
    );
    expect(
      within(note).getByRole('link', { name: 'Check them' })
    ).toHaveAttribute('href', '/w/acme/p/prod_home/ideas?check=1');
    expect(startButton()).toBeDisabled();
  });

  it('kept ads changed since the quote: says so and refreshes on request', async () => {
    mockGateway.startMarketingTest.mockResolvedValue(
      refused('test_quote_changed')
    );
    renderScreen();
    await answer();
    await waitFor(() => expect(startButton()).toBeEnabled());
    fireEvent.click(startButton());
    const changed = await screen.findByTestId('take-changed');
    expect(changed).toHaveTextContent(
      'The kept ads or the quote changed since you looked.'
    );
    const ideasReads = mockGateway.getMarketingIdeas.mock.calls.length;
    const quoteReads = mockGateway.getMarketingTestQuote.mock.calls.length;
    fireEvent.click(within(changed).getByRole('button', { name: 'Refresh' }));
    await waitFor(() =>
      expect(mockGateway.getMarketingIdeas.mock.calls.length).toBeGreaterThan(
        ideasReads
      )
    );
    await waitFor(() =>
      expect(
        mockGateway.getMarketingTestQuote.mock.calls.length
      ).toBeGreaterThan(quoteReads)
    );
    await waitFor(() =>
      expect(screen.queryByTestId('take-changed')).toBeNull()
    );
  });
});

describe('TakeToTest: after the start (#256)', () => {
  it('starts with the quote’s hash, answers and kept ads, then follows the checks', async () => {
    mockGateway.startMarketingTest.mockResolvedValue(ok(TEST, 201));
    mockGateway.getMarketingRound.mockResolvedValue(
      envelope(testRound(['running', 'queued']))
    );
    renderScreen();
    await answer();
    await waitFor(() => expect(startButton()).toBeEnabled());
    fireEvent.click(startButton());

    expect(await screen.findByText('0 of 2 have a result')).toBeInTheDocument();
    expect(mockGateway.startMarketingTest).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      actionInstanceId: expect.any(String),
      projectId: 'mp_secret',
      expectedRevision: 3,
      body: {
        answers: expect.objectContaining({
          target_cost: '30',
          round_budget: '1200',
          duration_days: 7,
        }),
        quote_hash: 'a'.repeat(64),
        asset_version_ids: ['av_1', 'av_3'],
      },
    });
    expect(mockGateway.getMarketingRound).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      roundId: 'mr_1',
      includeQcKinds: true,
    });
    expect(mockGateway.completeMarketingTestStart).not.toHaveBeenCalled();
  });

  it('checks did not pass: only those ads open, and a reason for each makes the package', async () => {
    mockGateway.listMarketingTests.mockResolvedValue(
      ok({
        product_id: 'prod_home',
        tests: [{ ...TEST, rounds: [] }],
        earlier_tests: [],
      })
    );
    const checked = testRound(['succeeded', 'succeeded']);
    mockGateway.getMarketingRound.mockResolvedValue(envelope(checked));
    mockGateway.completeMarketingTestStart
      .mockResolvedValueOnce(
        refused('check_findings_open', {
          findings: [
            {
              asset_version_id: 'av_3',
              check_id: 'qc_3',
              finding_ids: ['comparison_faithful'],
            },
          ],
        })
      )
      .mockResolvedValueOnce(
        ok({
          test_id: 'mp_test',
          round_id: 'mr_1',
          handoff_id: 'mh_1',
          handoff_state: 'queued',
          reviews: [],
        })
      );
    renderScreen();

    const open = await screen.findByTestId('test-open-findings');
    expect(mockGateway.completeMarketingTestStart).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      actionInstanceId: expect.any(String),
      testId: 'mp_test',
      expectedRevision: 4,
      body: { accepted_findings: [] },
    });
    // Only the ad with a problem opens, its finding in words.
    const ads = within(open).getAllByTestId('test-open-ad');
    expect(ads).toHaveLength(1);
    expect(ads[0]).toHaveTextContent('Rain or shine.');
    expect(ads[0]).toHaveTextContent(
      'What changes and what stays the same match what this round compares'
    );
    expect(ads[0]).toHaveTextContent('It looks like the other claim.');
    expect(open).not.toHaveTextContent('Plug in. Go to bed.');

    const accept = within(open).getByRole('button', {
      name: 'This is fine, carry on',
    });
    expect(accept).toBeDisabled();
    fireEvent.change(
      within(ads[0]).getByLabelText('Why is this ad fine as it is?'),
      { target: { value: 'Both claims are told apart by the headline.' } }
    );
    mockGateway.getMarketingRound.mockResolvedValue(
      envelope({ ...checked, handoff: handoff('ready') } as TestRound)
    );
    fireEvent.click(accept);

    expect(
      await screen.findByRole('heading', { name: 'The ad package is ready' })
    ).toBeInTheDocument();
    expect(mockGateway.completeMarketingTestStart).toHaveBeenLastCalledWith(
      expect.objectContaining({
        expectedRevision: 4,
        body: {
          accepted_findings: [
            {
              asset_version_id: 'av_3',
              check_id: 'qc_3',
              finding_ids: ['comparison_faithful'],
              note: 'Both claims are told apart by the headline.',
            },
          ],
        },
      })
    );
  });

  it('every check passed: makes the package by itself when the page opens again, and downloads it', async () => {
    mockGateway.listMarketingTests.mockResolvedValue(
      ok({ product_id: 'prod_home', tests: [TEST], earlier_tests: [] })
    );
    const passed = testRound(['succeeded', 'succeeded']);
    passed.assets[1].paired_ai_qc!.result = {
      findings: [finding('comparison_faithful', 'pass')],
    } as never;
    mockGateway.getMarketingRound
      .mockResolvedValueOnce(envelope(passed))
      .mockResolvedValue(
        envelope({ ...passed, handoff: handoff('ready') } as TestRound)
      );
    mockGateway.completeMarketingTestStart.mockResolvedValue(
      ok({
        test_id: 'mp_test',
        round_id: 'mr_1',
        handoff_id: 'mh_1',
        handoff_state: 'queued',
        reviews: [],
      })
    );
    mockGateway.downloadMarketingHandoff.mockResolvedValue(
      ok(
        {
          download_grant: { url: 'https://storage.example/package.zip' },
        },
        201
      )
    );
    renderScreen();

    const pack = await screen.findByTestId('test-package');
    // No question asked again: the page went straight to the test.
    expect(screen.queryByTestId('take-start')).toBeNull();
    expect(mockGateway.completeMarketingTestStart).toHaveBeenCalledTimes(1);
    expect(pack).toHaveTextContent('2 ads in the package');
    expect(pack).toHaveTextContent('Ad name: MA-7Q2 · Rain or shine.');
    fireEvent.click(
      within(pack).getByRole('button', { name: 'Download the ad package' })
    );
    expect(
      await within(pack).findByRole('link', { name: 'Open the download' })
    ).toHaveAttribute('href', 'https://storage.example/package.zip');
    expect(mockGateway.downloadMarketingHandoff).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      actionInstanceId: expect.any(String),
      roundId: 'mr_1',
    });
  });

  it('cancels before the package, after confirming, and goes back to change the ads', async () => {
    mockGateway.listMarketingTests.mockResolvedValue(
      ok({ product_id: 'prod_home', tests: [TEST], earlier_tests: [] })
    );
    mockGateway.getMarketingRound.mockResolvedValue(
      envelope(testRound(['succeeded', 'failed']))
    );
    mockGateway.completeMarketingTestStart.mockResolvedValue(
      refused('check_evidence_missing', {
        ads: [
          {
            asset_version_id: 'av_3',
            check_ids: ['comparison_faithful'],
            reason: 'check_failed',
          },
        ],
      })
    );
    mockGateway.cancelMarketingTest.mockResolvedValue(
      ok({ ...TEST, revision: 2, current_round_id: null })
    );
    renderScreen();

    expect(
      await screen.findByText(
        'Its look-together check didn’t finish.'.replace('’', "'")
      )
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole('button', { name: 'Cancel the test and change the ads' })
    );
    expect(mockGateway.cancelMarketingTest).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole('button', { name: 'Yes, cancel the test' })
    );
    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith('/w/acme/p/prod_home/ideas')
    );
    expect(mockGateway.cancelMarketingTest).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      actionInstanceId: expect.any(String),
      testId: 'mp_test',
      expectedRevision: 4,
      body: {},
    });
  });

  it('a second test of the product is refused: carries on with the one in progress', async () => {
    mockGateway.startMarketingTest.mockResolvedValue(
      refused('test_in_progress', { test_id: 'mp_test' })
    );
    mockGateway.getMarketingRound.mockResolvedValue(
      envelope(testRound(['running', 'running']))
    );
    renderScreen();
    await answer();
    await waitFor(() => expect(startButton()).toBeEnabled());
    fireEvent.click(startButton());
    expect(await screen.findByText('0 of 2 have a result')).toBeInTheDocument();
    expect(mockGateway.getMarketingTest).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      testId: 'mp_test',
    });
  });
});

describe('TakeToTest: nothing to take', () => {
  it('points back to the ad ideas when nothing is kept', async () => {
    mockGateway.getMarketingIdeas.mockResolvedValue(
      ok(
        ideasDetail({
          ads: KEPT.ads.map((item) => ({ ...item, kept: false })),
        })
      )
    );
    renderScreen();
    const none = await screen.findByTestId('take-no-kept');
    expect(none).toHaveTextContent('No ads are kept yet.');
    expect(
      within(none).getByRole('link', { name: 'Back to the ad ideas' })
    ).toHaveAttribute('href', '/w/acme/p/prod_home/ideas');
  });
});
