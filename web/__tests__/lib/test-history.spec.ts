import {
  datesMoved,
  earlierTestHref,
  historyTestHref,
  localDay,
  needsResult,
  productTestState,
  runningTest,
  testStatus,
  uploader,
  type HistoryRound,
  type HistoryTest,
  type TestHistory,
} from '@/lib/tests/test-history';
import type { TestResult } from '@/lib/tests/test-in-progress';

/**
 * What the test history and the overview read from a product's tests
 * (design §5.10, #258): the test in progress, whether its report is missing,
 * whether a result waits to be read, and where each test opens.
 */

const round = (extra: Partial<HistoryRound> = {}): HistoryRound => ({
  id: 'mr_1',
  ordinal: 1,
  state: 'handoff',
  start_date: '2026-11-26',
  end_date: '2026-12-02',
  effective_start_date: '2026-11-26',
  effective_end_date: '2026-12-02',
  reports: [],
  ...extra,
});

const historyTest = (extra: Partial<HistoryTest> = {}): HistoryTest => ({
  id: 'mt_1',
  source_project_id: 'mp_ideas',
  comparison: 'claims',
  plan_summary: {
    target_cost: '30.00',
    round_budget: '1200.00',
    duration_days: 7,
    currency: 'USD',
    landing_page: 'https://acmecharge.example/home',
  },
  rounds: [round()],
  current_round_id: 'mr_1',
  created_at: '2026-11-25T09:00:00Z',
  ...extra,
});

const history = (tests: HistoryTest[]): TestHistory => ({
  product_id: 'prod_home',
  tests,
  earlier_tests: [],
});

const report = {
  batch_id: 'mmb_1',
  kind: 'upload' as const,
  replaces_batch_id: null,
  uploaded_at: '2026-11-28T10:00:00Z',
  uploaded_by: 'user_1',
  row_count: 14,
};

const result = (extra: Partial<TestResult> = {}): TestResult =>
  ({
    test_id: 'mt_1',
    round_id: 'mr_1',
    mature: true,
    min_purchases: 30,
    scope: 'these_ads',
    evaluation_id: null,
    evaluated_at: null,
    current: false,
    comparisons: [],
    ...extra,
  }) as TestResult;

describe('runningTest: the product test in progress (plan E3)', () => {
  it('is the test with an open round, with that round', () => {
    const ended = historyTest({
      id: 'mt_old',
      current_round_id: null,
      rounds: [round({ id: 'mr_old', state: 'closed' })],
    });
    const found = runningTest(history([historyTest(), ended]));
    expect(found?.test.id).toBe('mt_1');
    expect(found?.round.id).toBe('mr_1');
  });

  it('is none when every test has ended', () => {
    expect(
      runningTest(
        history([
          historyTest({
            current_round_id: null,
            rounds: [round({ state: 'closed' })],
          }),
        ])
      )
    ).toBeNull();
  });
});

describe('productTestState (#258)', () => {
  const today = '2026-11-28';

  it('names no test when none is in progress', () => {
    expect(productTestState(history([]), null, today)).toBeNull();
  });

  it('counts a day of delivery only after the first day', () => {
    expect(
      productTestState(history([historyTest()]), null, '2026-11-26')?.started
    ).toBe(false);
    expect(
      productTestState(history([historyTest()]), null, '2026-11-27')?.started
    ).toBe(true);
  });

  it('follows the effective start, not the frozen one', () => {
    const moved = historyTest({
      rounds: [round({ effective_start_date: '2026-11-29' })],
    });
    expect(productTestState(history([moved]), null, today)?.started).toBe(
      false
    );
  });

  it('a round still in its checks has no package yet', () => {
    const state = productTestState(
      history([historyTest({ rounds: [round({ state: 'reviewing' })] })]),
      null,
      today
    );
    expect(state).toMatchObject({ packageMade: false, reportCount: 0 });
  });

  it('a result is unseen once it is due and nobody read it, or the data changed since', () => {
    const reported = history([
      historyTest({ rounds: [round({ reports: [report] })] }),
    ]);
    expect(productTestState(reported, result(), today)?.resultUnseen).toBe(
      true
    );
    expect(
      productTestState(
        reported,
        result({ evaluation_id: 'me_1', current: false }),
        today
      )?.resultUnseen
    ).toBe(true);
    expect(
      productTestState(
        reported,
        result({ evaluation_id: 'me_1', current: true }),
        today
      )?.resultUnseen
    ).toBe(false);
    expect(
      productTestState(reported, result({ mature: false }), today)?.resultUnseen
    ).toBe(false);
  });

  it('ignores a result of another round, or none read', () => {
    const reported = history([
      historyTest({ rounds: [round({ reports: [report] })] }),
    ]);
    expect(
      productTestState(reported, result({ round_id: 'mr_other' }), today)
        ?.resultUnseen
    ).toBe(false);
    expect(productTestState(reported, null, today)?.resultUnseen).toBe(false);
  });
});

describe('needsResult: the overview reads a result only once a report is in', () => {
  it('reads none before the package or before a report', () => {
    expect(needsResult(null)).toBe(false);
    expect(
      needsResult({ test: historyTest(), round: round({ state: 'reviewing' }) })
    ).toBe(false);
    expect(needsResult({ test: historyTest(), round: round() })).toBe(false);
  });

  it('reads it once a report is in', () => {
    expect(
      needsResult({
        test: historyTest(),
        round: round({ state: 'awaiting_data', reports: [report] }),
      })
    ).toBe(true);
  });
});

describe('where a test opens', () => {
  it('opens the take-to-test screen during its checks, its own page otherwise', () => {
    expect(
      historyTestHref(
        'acme',
        'prod_home',
        historyTest({ rounds: [round({ state: 'reviewing' })] })
      )
    ).toBe('/w/acme/p/prod_home/take-to-test');
    expect(historyTestHref('acme', 'prod_home', historyTest())).toBe(
      '/w/acme/p/prod_home/tests/mt_1'
    );
    expect(
      historyTestHref(
        'acme',
        'prod_home',
        historyTest({
          current_round_id: null,
          rounds: [round({ state: 'closed' })],
        })
      )
    ).toBe('/w/acme/p/prod_home/tests/mt_1');
  });

  it('opens an earlier test on the earlier tests page', () => {
    expect(earlierTestHref('acme', 'mp/1')).toBe(
      '/w/acme/experiments?test=mp%2F1'
    );
  });

  it('says where each test stands', () => {
    expect(
      testStatus(historyTest({ rounds: [round({ state: 'reviewing' })] }))
    ).toBe('setting_up');
    expect(testStatus(historyTest())).toBe('running');
    expect(
      testStatus(
        historyTest({
          current_round_id: null,
          rounds: [round({ state: 'closed' })],
        })
      )
    ).toBe('ended');
  });
});

test('datesMoved: a later start or an early end shows the planned days too', () => {
  expect(datesMoved(round())).toBe(false);
  expect(datesMoved(round({ effective_start_date: '2026-11-27' }))).toBe(true);
  expect(datesMoved(round({ effective_end_date: '2026-11-30' }))).toBe(true);
});

test('uploader: the reader, a named member, another member, or nobody', () => {
  const names = new Map([['user_2', 'Avery Example']]);
  expect(uploader('user_1', 'user_1', names)).toEqual({ kind: 'you' });
  expect(uploader('user_2', 'user_1', names)).toEqual({
    kind: 'member',
    name: 'Avery Example',
  });
  expect(uploader('user_3', 'user_1', names)).toEqual({ kind: 'other' });
  expect(uploader(null, 'user_1', names)).toBeNull();
});

test('localDay is the reader’s calendar day', () => {
  expect(localDay(new Date(2026, 0, 5, 23, 30))).toBe('2026-01-05');
});
