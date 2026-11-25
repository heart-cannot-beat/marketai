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
import { TestInProgress } from '@/components/tests/TestInProgress';
import type { TestRound } from '@/lib/tests/take-to-test';
import type {
  BindingSuggestions,
  TestProgress,
  TestReport,
  TestResult,
} from '@/lib/tests/test-in-progress';

/**
 * "Test in progress" (design §5.7–§5.9, §5.11, #257): upload the full
 * report, match the Meta ads (prefilled, confirmed once), the first
 * report's start date, the numbers so far (not a conclusion), what ran
 * differently, the round's days, the ads changed after the start, and the
 * result once it is due. Every number on the page is the server's.
 */
jest.mock('next/router', () => ({
  useRouter: () => ({
    asPath: '/w/acme/p/prod_home/tests/mp_test',
    query: {},
    push: jest.fn(),
    replace: jest.fn(),
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'ws_1', role: 'MEMBER' },
  }),
}));
const mockGateway = {
  getMarketingTest: jest.fn(),
  getMarketingRound: jest.fn(),
  getMarketingTestProgress: jest.fn(),
  getMarketingTestResult: jest.fn(),
  previewMarketingTestReport: jest.fn(),
  suggestMarketingTestBindings: jest.fn(),
  confirmMarketingTestBindings: jest.fn(),
  confirmMarketingTestReport: jest.fn(),
  changeMarketingTestStart: jest.fn(),
  endMarketingTestRoundEarly: jest.fn(),
  recordMarketingTestDeviation: jest.fn(),
  evaluateMarketingRound: jest.fn(),
  downloadMarketingHandoff: jest.fn(),
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

const TEST = {
  id: 'mp_test',
  revision: 1,
  product_id: 'prod_home',
  source_project_id: 'mp_ideas',
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
      state: 'handoff',
      start_date: '2026-11-26',
      end_date: '2026-12-02',
      effective_start_date: '2026-11-26',
      effective_end_date: '2026-12-02',
    },
  ],
  current_round_id: 'mr_1',
  created_at: '2026-11-25T08:00:00+00:00',
};

const ENTRIES = [
  {
    entry_id: 'e1',
    asset_version_id: 'av_1',
    headline: 'Plug in. Go to bed.',
    ad_name: 'MA-7Q2 · Plug in. Go to bed.',
    display_name: null,
  },
  {
    entry_id: 'e3',
    asset_version_id: 'av_3',
    headline: 'Rain or shine.',
    ad_name: 'MA-7Q2 · Rain or shine.',
    display_name: null,
  },
];

function testRound(extra: Partial<TestRound> = {}): TestRound {
  return {
    id: 'mr_1',
    ordinal: 1,
    state: 'handoff',
    revision: 7,
    timezone: 'America/Los_Angeles',
    snapshot: { currency: 'USD', attribution_window: '7d_click_1d_view' },
    assets: [],
    handoff: {
      id: 'mh_1',
      state: 'ready',
      error_code: null,
      manifest_hash: 'h',
      quality_current: true,
      entries: ENTRIES,
    },
    bindings: [],
    batches: [],
    ...extra,
  } as unknown as TestRound;
}

const group = (
  id: string,
  label: string,
  versions: string[],
  extra: Record<string, unknown> = {}
) => ({
  id,
  label,
  asset_version_ids: versions,
  spend: '0.00',
  purchases: '0',
  cost_per_purchase: null,
  remaining_purchases: 20,
  conditions_changed: false,
  ...extra,
});

function progress(extra: Partial<TestProgress> = {}): TestProgress {
  return {
    test_id: 'mp_test',
    round_id: 'mr_1',
    revision: 7,
    provisional: true,
    currency: 'USD',
    target_cost: '30.00',
    min_purchases: 20,
    effective_start_date: '2026-11-26',
    effective_end_date: '2026-12-02',
    result_date: '2026-12-11',
    data_through: null,
    comparisons: [
      group('g1', 'Plug in at night and forget about it', ['av_1']),
      group('g2', 'Safe outdoors', ['av_3']),
    ],
    deviations: [],
    changed_ads: [],
    ...extra,
  } as TestProgress;
}

function schedule(extra: Record<string, unknown> = {}) {
  return {
    test_id: 'mp_test',
    round_id: 'mr_1',
    revision: 7,
    start_date: '2026-11-26',
    end_date: '2026-12-02',
    effective_start_date: '2026-11-26',
    effective_end_date: '2026-12-02',
    ended_early: false,
    result_date: '2026-12-11',
    start_movable: true,
    changes: [],
    ...extra,
  };
}

function result(extra: Partial<TestResult> = {}): TestResult {
  return {
    test_id: 'mp_test',
    round_id: 'mr_1',
    schedule: schedule(),
    mature: false,
    min_purchases: 20,
    scope: 'these_ads',
    evaluation_id: null,
    evaluated_at: null,
    current: false,
    comparisons: [],
    ...extra,
  } as TestResult;
}

function report(extra: Partial<TestReport> = {}): TestReport {
  return {
    id: 'mmb_1',
    round_id: 'mr_1',
    state: 'preview',
    filename: 'acme-charge-report.csv',
    raw_hash: 'r',
    created_at: '2026-11-30T08:00:00+00:00',
    ad_account_id: '1234',
    account_timezone: 'America/Los_Angeles',
    currency: 'USD',
    attribution_window: '7d_click_1d_view',
    first_date: '2026-11-26',
    last_date: '2026-11-29',
    row_count: 9,
    issues: [],
    counted_rows: 0,
    replaces_rows: 0,
    replaced_rows: 0,
    excluded: [],
    ...extra,
  } as TestReport;
}

const SUGGESTIONS: BindingSuggestions = {
  round_id: 'mr_1',
  batch_id: 'mmb_1',
  ad_account_id: '1234',
  ad_name_column: 'Ad name',
  locked: false,
  entries: ENTRIES.map((e) => ({
    entry_id: e.entry_id,
    ad_name: e.ad_name,
    display_name: null,
    asset_version_id: e.asset_version_id,
    asset_code: `MK-${e.entry_id}`,
  })),
  ads: [
    {
      ad_id: '2001',
      ad_names: ['MA-7Q2 · Plug in. Go to bed.'],
      status: 'suggested',
      entry_id: 'e1',
      key_kind: 'ma_key',
      candidate_entry_ids: ['e1'],
      other_round_ids: [],
    },
    {
      ad_id: '2003',
      ad_names: ['Rain ad (renamed)'],
      status: 'no_match',
      entry_id: null,
      key_kind: null,
      candidate_entry_ids: [],
      other_round_ids: [],
    },
    {
      ad_id: '2999',
      ad_names: ['Brightvolt retargeting'],
      status: 'no_match',
      entry_id: null,
      key_kind: null,
      candidate_entry_ids: [],
      other_round_ids: [],
    },
  ],
} as BindingSuggestions;

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
      <TestInProgress productId="prod_home" testId="mp_test" />
    </Fresh>
  );
}

const CSV =
  'Day,Ad ID,Ad name,Amount spent (USD),Purchases\n2026-11-26,2001,MA-7Q2 · Plug in. Go to bed.,40,1\n';

async function upload(account = '1234') {
  const input = (await screen.findByLabelText(
    'Report file (CSV)'
  )) as HTMLInputElement;
  const file = new File([CSV], 'acme-charge-report.csv', { type: 'text/csv' });
  Object.defineProperty(file, 'text', { value: () => Promise.resolve(CSV) });
  fireEvent.change(input, { target: { files: [file] } });
  fireEvent.change(screen.getByLabelText('Meta ad account ID'), {
    target: { value: account },
  });
  await waitFor(() =>
    expect(screen.getByTestId('report-upload-button')).toBeEnabled()
  );
  fireEvent.click(screen.getByTestId('report-upload-button'));
}

beforeEach(() => {
  jest.clearAllMocks();
  mockGateway.getMarketingTest.mockResolvedValue(ok(TEST));
  mockGateway.getMarketingRound.mockResolvedValue(envelope(testRound()));
  mockGateway.getMarketingTestProgress.mockResolvedValue(ok(progress()));
  mockGateway.getMarketingTestResult.mockResolvedValue(ok(result()));
});

describe('TestInProgress: the report comes back (#257)', () => {
  it('uploads the full report, prefills the matches, confirms them once, then counts the report', async () => {
    mockGateway.previewMarketingTestReport.mockResolvedValue(ok(report(), 201));
    mockGateway.suggestMarketingTestBindings.mockResolvedValue(ok(SUGGESTIONS));
    mockGateway.confirmMarketingTestBindings.mockResolvedValue(
      ok({ round_id: 'mr_1' })
    );
    mockGateway.confirmMarketingTestReport.mockResolvedValue(
      ok(
        report({
          state: 'confirmed',
          counted_rows: 6,
          excluded: [
            {
              ad_id: '2999',
              reason: 'other_ad',
              first_date: '2026-11-26',
              last_date: '2026-11-28',
              rows: 3,
              spend: '15.00',
            },
          ],
        })
      )
    );
    renderScreen();
    expect(
      await screen.findByRole('heading', { name: 'Test in progress' })
    ).toBeInTheDocument();
    await upload('act_1234');

    expect(mockGateway.previewMarketingTestReport).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        testId: 'mp_test',
        roundId: 'mr_1',
        expectedRevision: 7,
        body: {
          csv_text: CSV,
          filename: 'acme-charge-report.csv',
          ad_account_id: 'act_1234',
          // The test's own settings unless the user says otherwise.
          account_timezone: 'America/Los_Angeles',
          currency: 'USD',
          attribution_window: '7d_click_1d_view',
          source_mode: 'customer_file',
          field_mapping: {},
        },
      })
    );
    const bind = await screen.findByTestId('binding-confirm');
    expect(mockGateway.suggestMarketingTestBindings).toHaveBeenCalledWith(
      expect.objectContaining({ body: { batch_id: 'mmb_1' } })
    );
    const rows = within(bind).getAllByTestId('binding-row');
    // The key in the first name filled it in; the other two wait for the user.
    expect(within(rows[0]).getByRole('combobox')).toHaveValue('e1');
    expect(
      within(rows[0]).getByText('Filled in from the package key in its name.')
    ).toBeInTheDocument();
    expect(within(rows[1]).getByRole('combobox')).toHaveValue('');
    expect(
      within(bind).getByText('2 Meta ads need you to choose or skip.')
    ).toBeInTheDocument();
    // Nothing is stored before the one confirmation.
    expect(mockGateway.confirmMarketingTestBindings).not.toHaveBeenCalled();
    fireEvent.change(within(rows[1]).getByRole('combobox'), {
      target: { value: 'e3' },
    });
    fireEvent.click(
      within(bind).getByRole('button', { name: 'Confirm 2 matches' })
    );
    await waitFor(() =>
      expect(mockGateway.confirmMarketingTestBindings).toHaveBeenCalledTimes(1)
    );
    expect(mockGateway.confirmMarketingTestBindings).toHaveBeenCalledWith(
      expect.objectContaining({
        expectedRevision: 7,
        body: {
          ad_account_id: '1234',
          bindings: [
            { ad_id: '2001', entry_id: 'e1' },
            { ad_id: '2003', entry_id: 'e3' },
          ],
        },
      })
    );

    // The report's first day is the start: nothing to ask.
    expect(screen.queryByTestId('start-date-question')).not.toBeInTheDocument();
    fireEvent.click(await screen.findByTestId('report-confirm-button'));
    await waitFor(() =>
      expect(mockGateway.confirmMarketingTestReport).toHaveBeenCalledWith(
        expect.objectContaining({ body: { batch_id: 'mmb_1' } })
      )
    );
    const counted = await screen.findByTestId('report-counted');
    expect(counted).toHaveTextContent('acme-charge-report.csv counts: 6 rows.');
    // Rows of an ad outside the test are kept but listed as not counted.
    expect(within(counted).getByTestId('report-excluded')).toHaveTextContent(
      "Meta ad 2999: 3 rows from November 26, 2026 to November 28, 2026 ($15). It is not one of this test's ads."
    );
  });

  it('on the first report, asks to move the start to the day the ads went live', async () => {
    mockGateway.previewMarketingTestReport.mockResolvedValue(
      ok(report({ first_date: '2026-11-28' }), 201)
    );
    mockGateway.suggestMarketingTestBindings.mockResolvedValue(
      ok({ ...SUGGESTIONS, ads: [SUGGESTIONS.ads[0]] })
    );
    mockGateway.confirmMarketingTestBindings.mockResolvedValue(
      ok({ round_id: 'mr_1' })
    );
    mockGateway.changeMarketingTestStart.mockResolvedValue(
      ok(schedule({ effective_start_date: '2026-11-28' }))
    );
    renderScreen();
    await upload();
    fireEvent.click(
      await screen.findByRole('button', { name: 'Confirm 1 match' })
    );
    const question = await screen.findByTestId('start-date-question');
    expect(question).toHaveTextContent(
      'It looks like you started on November 28, 2026. Change the start to November 28, 2026?'
    );
    // Not counted until the question is answered.
    expect(
      screen.queryByTestId('report-confirm-button')
    ).not.toBeInTheDocument();

    mockGateway.getMarketingTestResult.mockResolvedValue(
      ok(
        result({
          schedule: schedule({
            effective_start_date: '2026-11-28',
            effective_end_date: '2026-12-04',
            result_date: '2026-12-13',
          }),
        })
      )
    );
    mockGateway.getMarketingRound.mockResolvedValue(
      envelope(testRound({ revision: 9 }))
    );
    fireEvent.click(
      within(question).getByRole('button', {
        name: 'Start on November 28, 2026',
      })
    );
    await waitFor(() =>
      expect(mockGateway.changeMarketingTestStart).toHaveBeenCalledWith(
        expect.objectContaining({
          expectedRevision: 7,
          body: { start_date: '2026-11-28', reason: null },
        })
      )
    );
    expect(await screen.findByTestId('report-confirm-button')).toBeEnabled();
    expect(screen.queryByTestId('start-date-question')).not.toBeInTheDocument();
    // The days read again: the schedule shows the moved start.
    expect(screen.getByTestId('test-schedule')).toHaveTextContent(
      'Planned: November 26, 2026'
    );
  });

  it('keeping the planned start counts the report as it is', async () => {
    mockGateway.previewMarketingTestReport.mockResolvedValue(
      ok(report({ first_date: '2026-11-28' }), 201)
    );
    mockGateway.suggestMarketingTestBindings.mockResolvedValue(
      ok({
        ...SUGGESTIONS,
        ads: [{ ...SUGGESTIONS.ads[0], status: 'bound' }],
      })
    );
    renderScreen();
    await upload();
    // Already matched: nothing to write.
    fireEvent.click(
      await screen.findByRole('button', { name: 'These matches are right' })
    );
    expect(mockGateway.confirmMarketingTestBindings).not.toHaveBeenCalled();
    fireEvent.click(
      await screen.findByRole('button', { name: 'Keep the planned start' })
    );
    expect(await screen.findByTestId('report-confirm-button')).toBeEnabled();
    expect(mockGateway.changeMarketingTestStart).not.toHaveBeenCalled();
  });

  it('says plainly when the report’s currency or attribution is not the test’s, and stops there', async () => {
    mockGateway.previewMarketingTestReport.mockResolvedValue(
      ok(
        report({
          currency: 'EUR',
          issues: [
            {
              row: 0,
              field: 'currency',
              code: 'report_currency_mismatch',
              params: { expected: 'USD', found: 'EUR' },
            },
            {
              row: 0,
              field: 'attribution_window',
              code: 'report_attribution_mismatch',
              params: { expected: '7d_click_1d_view', found: '1d_click' },
            },
          ],
        } as never),
        201
      )
    );
    renderScreen();
    await upload();
    const preview = await screen.findByTestId('report-preview');
    expect(preview).toHaveTextContent(
      'This report is in EUR; this test was set up in USD.'
    );
    expect(preview).toHaveTextContent(
      'This report uses the Ordered within 1 day of a click attribution window'
    );
    expect(preview).toHaveTextContent('This report cannot count as it is.');
    expect(mockGateway.suggestMarketingTestBindings).not.toHaveBeenCalled();
    expect(
      screen.queryByTestId('report-confirm-button')
    ).not.toBeInTheDocument();
    fireEvent.click(
      within(preview).getByRole('button', { name: 'Choose another file' })
    );
    expect(
      await screen.findByLabelText('Report file (CSV)')
    ).toBeInTheDocument();
  });

  it('a later full report says it replaces the days it repeats', async () => {
    mockGateway.getMarketingRound.mockResolvedValue(
      envelope(
        testRound({
          state: 'awaiting_data',
          bindings: [
            {
              id: 'b1',
              entry_id: 'e1',
              ad_account_id: '1234',
              ad_id: '2001',
              conditions_match: true,
              deviations: [],
            },
          ],
          batches: [
            {
              id: 'mmb_0',
              state: 'confirmed',
              raw_hash: 'x',
              metadata: {
                filename: 'first.csv',
                ad_account_id: '1234',
                account_timezone: 'America/Los_Angeles',
                currency: 'USD',
                attribution_window: '7d_click_1d_view',
                source_mode: 'customer_file',
                field_mapping: {},
              },
              row_count: 3,
              issues: [],
              replaces_batch_id: null,
            },
          ],
        } as never)
      )
    );
    mockGateway.getMarketingTestResult.mockResolvedValue(
      ok(result({ schedule: schedule({ start_movable: false }) }))
    );
    renderScreen();
    expect(
      await screen.findByText(/A newer report replaces the days it repeats/)
    ).toBeInTheDocument();
    // The account the round's ads are matched in is filled in.
    expect(screen.getByLabelText('Meta ad account ID')).toHaveValue('1234');
    expect(
      screen.getByText(
        'The start date can no longer change: a report already counts.'
      )
    ).toBeInTheDocument();
  });
});

describe('TestInProgress: the numbers so far (#257)', () => {
  it('marks them as not a conclusion and says how many orders are still missing', async () => {
    mockGateway.getMarketingTestProgress.mockResolvedValue(
      ok(
        progress({
          data_through: '2026-11-29',
          comparisons: [
            group('g1', 'Plug in at night and forget about it', ['av_1'], {
              spend: '120.00',
              purchases: '5',
              cost_per_purchase: '24.00',
              remaining_purchases: 15,
            }),
            group('g2', 'Safe outdoors', ['av_3'], {
              spend: '80.00',
              purchases: '0',
              remaining_purchases: 20,
              conditions_changed: true,
            }),
          ],
        })
      )
    );
    renderScreen();
    const numbers = await screen.findByTestId('test-numbers');
    expect(within(numbers).getByText('Not a conclusion')).toBeInTheDocument();
    expect(numbers).toHaveTextContent(
      'Counted reports run to November 29, 2026. These numbers will change; the result comes from December 11, 2026.'
    );
    const [first, second] =
      within(numbers).getAllByTestId('numbers-comparison');
    expect(first).toHaveTextContent('$120');
    expect(first).toHaveTextContent('$24');
    expect(first).toHaveTextContent(
      '15 more orders needed for a verdict (at least 20).'
    );
    expect(second).toHaveTextContent('No orders yet');
    expect(second).toHaveTextContent(
      'Something ran differently from the plan: no conclusion on this group this round.'
    );
    // Reading the numbers evaluates nothing.
    expect(mockGateway.evaluateMarketingRound).not.toHaveBeenCalled();
  });

  it('warns which groups draw no conclusion before a deviation is recorded', async () => {
    mockGateway.recordMarketingTestDeviation.mockResolvedValue(
      ok(progress(), 201)
    );
    renderScreen();
    fireEvent.click(
      await screen.findByRole('button', {
        name: 'Record something that ran differently',
      })
    );
    const form = screen.getByTestId('deviation-form');
    fireEvent.click(
      within(form).getByRole('checkbox', { name: /Rain or shine/ })
    );
    fireEvent.change(within(form).getByLabelText('What was different?'), {
      target: { value: 'Budget doubled on day 3 in Meta' },
    });
    fireEvent.click(within(form).getByRole('button', { name: 'Record it' }));
    // First the consequence, nothing sent yet.
    expect(within(form).getByTestId('deviation-warning')).toHaveTextContent(
      'Once recorded, this round draws no conclusion on Safe outdoors.'
    );
    expect(mockGateway.recordMarketingTestDeviation).not.toHaveBeenCalled();
    fireEvent.click(
      within(form).getByRole('button', { name: 'Yes, record it' })
    );
    await waitFor(() =>
      expect(mockGateway.recordMarketingTestDeviation).toHaveBeenCalledWith(
        expect.objectContaining({
          expectedRevision: 7,
          body: {
            asset_version_ids: ['av_3'],
            note: 'Budget doubled on day 3 in Meta',
          },
        })
      )
    );
  });

  it('shows the ads changed after the start and still downloads the package', async () => {
    mockGateway.getMarketingTestProgress.mockResolvedValue(
      ok(
        progress({
          changed_ads: [
            {
              asset_version_id: 'av_3',
              changes: ['fact', 'copy'],
              fact_ids: ['f1'],
            },
          ],
        })
      )
    );
    mockGateway.downloadMarketingHandoff.mockResolvedValue(
      ok({ download_grant: { url: 'https://files.example/package.zip' } })
    );
    renderScreen();
    const changed = await screen.findByTestId('changed-ads');
    expect(changed).toHaveTextContent(
      'This ad was changed in the ad ideas after the test started'
    );
    expect(changed).toHaveTextContent(
      'A fact it cites was changed or removed.'
    );
    expect(changed).toHaveTextContent(
      'The ad in Meta still shows the old version: we suggest pausing it or making it again.'
    );
    fireEvent.click(screen.getByTestId('package-download'));
    expect(
      await screen.findByRole('link', { name: 'Open the download' })
    ).toHaveAttribute('href', 'https://files.example/package.zip');
  });
});

describe('TestInProgress: the round’s days (#257)', () => {
  it('moves the start before the first report counts', async () => {
    mockGateway.changeMarketingTestStart.mockResolvedValue(ok(schedule()));
    renderScreen();
    fireEvent.click(
      await screen.findByRole('button', { name: 'Change the start date' })
    );
    const form = screen.getByTestId('schedule-start-form');
    fireEvent.change(within(form).getByLabelText('New start date'), {
      target: { value: '2026-11-27' },
    });
    fireEvent.change(within(form).getByLabelText('Why (optional)'), {
      target: { value: 'Review in Meta took a day' },
    });
    fireEvent.click(
      within(form).getByRole('button', { name: 'Change the start' })
    );
    await waitFor(() =>
      expect(mockGateway.changeMarketingTestStart).toHaveBeenCalledWith(
        expect.objectContaining({
          body: {
            start_date: '2026-11-27',
            reason: 'Review in Meta took a day',
          },
        })
      )
    );
  });

  it('ends the round early after saying what it means, and shows a refusal', async () => {
    mockGateway.endMarketingTestRoundEarly.mockResolvedValue(
      refused('end_date_out_of_range', {
        earliest: '2026-11-26',
        latest: '2026-11-30',
      })
    );
    renderScreen();
    fireEvent.click(
      await screen.findByRole('button', { name: 'End this round early' })
    );
    const form = screen.getByTestId('schedule-end-form');
    fireEvent.change(within(form).getByLabelText('Last day the ads ran'), {
      target: { value: '2026-12-01' },
    });
    fireEvent.click(within(form).getByRole('button', { name: 'End early' }));
    expect(within(form).getByRole('alert')).toHaveTextContent(
      "The result will take December 1, 2026 as the round's last day"
    );
    expect(mockGateway.endMarketingTestRoundEarly).not.toHaveBeenCalled();
    fireEvent.click(
      within(form).getByRole('button', { name: 'Yes, end on this day' })
    );
    await waitFor(() =>
      expect(mockGateway.endMarketingTestRoundEarly).toHaveBeenCalledWith(
        expect.objectContaining({
          expectedRevision: 7,
          body: { end_date: '2026-12-01', reason: null },
        })
      )
    );
    expect(
      await within(form).findByText(/November 30, 2026|2026-11-30/)
    ).toBeInTheDocument();
  });
});

describe('TestInProgress: the result (#257)', () => {
  it('before the result day: says when, with no button', async () => {
    renderScreen();
    const section = await screen.findByTestId('test-result');
    expect(section).toHaveTextContent(
      'The result can be read from December 11, 2026'
    );
    expect(within(section).queryByTestId('see-result')).not.toBeInTheDocument();
  });

  it('once due, “See the result” evaluates the round, then shows each group for these ads only', async () => {
    mockGateway.getMarketingTestResult.mockResolvedValue(
      ok(result({ mature: true }))
    );
    mockGateway.evaluateMarketingRound.mockImplementation(async () => {
      mockGateway.getMarketingTestResult.mockResolvedValue(
        ok(
          result({
            mature: true,
            evaluation_id: 'ev_1',
            evaluated_at: '2026-12-11T09:00:00+00:00',
            current: true,
            comparisons: [
              {
                id: 'g1',
                label: 'Plug in at night and forget about it',
                status: 'meets_goal',
                purchases: '22',
                remaining_purchases: 0,
                asset_version_ids: ['av_1'],
              },
              {
                id: 'g2',
                label: 'Safe outdoors',
                status: 'not_clear',
                purchases: '6',
                remaining_purchases: 14,
                asset_version_ids: ['av_3'],
              },
            ],
          })
        )
      );
      return ok({});
    });
    renderScreen();
    const section = await screen.findByTestId('test-result');
    expect(section).toHaveTextContent(
      'Free. It reads the reports that count now.'
    );
    fireEvent.click(within(section).getByTestId('see-result'));
    await waitFor(() =>
      expect(mockGateway.evaluateMarketingRound).toHaveBeenCalledWith(
        expect.objectContaining({ roundId: 'mr_1', expectedRevision: 7 })
      )
    );
    const groups = await screen.findByTestId('result-comparisons');
    expect(groups).toHaveTextContent('This result is about these ads only.');
    expect(groups).toHaveTextContent('Meets the goal');
    expect(groups).toHaveTextContent("Can't tell yet");
    expect(groups).toHaveTextContent('14 orders short');
    expect(within(section).queryByTestId('see-result')).not.toBeInTheDocument();
  });
});

describe('TestInProgress: before the package (#257)', () => {
  it('points back to “take to test” while the package is not ready', async () => {
    mockGateway.getMarketingRound.mockResolvedValue(
      envelope(testRound({ state: 'reviewing', handoff: null } as never))
    );
    renderScreen();
    expect(
      await screen.findByRole('heading', {
        name: "The ad package isn't ready yet",
      })
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Carry on' })).toHaveAttribute(
      'href',
      '/w/acme/p/prod_home/take-to-test'
    );
    expect(mockGateway.getMarketingTestProgress).not.toHaveBeenCalled();
  });
});
