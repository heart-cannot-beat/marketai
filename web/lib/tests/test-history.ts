import type { components } from '@/lib/domain-client/generated/domain-v1';
import {
  takeToTestHref,
  testHref,
  type ProductTestState,
} from '@/lib/products/workspace-products';

/**
 * What the "test history" view and the overview read from a product's tests
 * (`list_marketing_tests`, design §5.10, §5.11, #258). Nothing here computes
 * a number: dates, reports and the result's state are the server's.
 */

export type TestHistory = components['schemas']['MarketingTestHistory'];
export type HistoryTest = components['schemas']['MarketingTestHistoryItem'];
export type EarlierTest = components['schemas']['MarketingEarlierTest'];
export type HistoryRound = components['schemas']['MarketingTestHistoryRound'];
export type HistoryReport = components['schemas']['MarketingTestReportUpload'];
type TestResult = components['schemas']['MarketingTestResult'];

/** A product with no card of its own has no tests (`product_not_found`). */
export function emptyHistory(productId: string): TestHistory {
  return { product_id: productId, tests: [], earlier_tests: [] };
}

export interface RunningTest {
  test: HistoryTest;
  round: HistoryRound;
}

/** The product's test in progress -- at most one (plan E3) -- and its open round. */
export function runningTest(history: TestHistory): RunningTest | null {
  for (const test of history.tests) {
    if (!test.current_round_id) continue;
    const round = test.rounds.find((item) => item.id === test.current_round_id);
    if (round) return { test, round };
  }
  return null;
}

/** The round is past its checks: its ad package is asked for or made. */
export function packageMade(round: HistoryRound): boolean {
  return round.state !== 'reviewing';
}

/**
 * Whether the overview reads the round's result: only once a report is in.
 * Before that the step is the missing report (or the test simply running),
 * whatever the result says.
 */
export function needsResult(running: RunningTest | null): boolean {
  return Boolean(
    running && packageMade(running.round) && running.round.reports.length > 0
  );
}

/**
 * The product's test in progress as the overview counts it. `today` is the
 * reader's calendar day (`YYYY-MM-DD`); `result` is the round's
 * `get_marketing_test_result`, or `null` when it was not read.
 */
export function productTestState(
  history: TestHistory,
  result: TestResult | null,
  today: string
): ProductTestState | null {
  const running = runningTest(history);
  if (!running) return null;
  const { test, round } = running;
  return {
    testId: test.id,
    packageMade: packageMade(round),
    started: today > round.effective_start_date,
    reportCount: round.reports.length,
    resultUnseen: Boolean(
      result &&
      result.round_id === round.id &&
      result.mature &&
      (!result.evaluation_id || !result.current)
    ),
  };
}

/** The reader's calendar day, `YYYY-MM-DD`. */
export function localDay(now: Date = new Date()): string {
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

export type TestStatus = 'setting_up' | 'running' | 'ended';

/** Where a test stands: its checks still running, in progress, or ended. */
export function testStatus(test: HistoryTest): TestStatus {
  if (!test.current_round_id) return 'ended';
  const round = test.rounds.find((item) => item.id === test.current_round_id);
  return round && !packageMade(round) ? 'setting_up' : 'running';
}

/**
 * Where a test opens: the "take to test" screen while its checks run (its
 * progress, findings and package are there), its own page otherwise.
 */
export function historyTestHref(
  slug: string,
  productId: string,
  test: HistoryTest
): string {
  return testStatus(test) === 'setting_up'
    ? takeToTestHref(slug, productId)
    : testHref(slug, productId, test.id);
}

/** An earlier test opens on the earlier tests page, as before. */
export function earlierTestHref(slug: string, projectId: string): string {
  return `/w/${slug}/experiments?test=${encodeURIComponent(projectId)}`;
}

/** The round's dates moved (a later start or an early end): show the planned ones too. */
export function datesMoved(round: HistoryRound): boolean {
  return (
    round.effective_start_date !== round.start_date ||
    round.effective_end_date !== round.end_date
  );
}

export type Uploader =
  | { kind: 'you' }
  | { kind: 'member'; name: string }
  | { kind: 'other' };

/**
 * Who sent a report: the reader, a member the reader can name (an Owner
 * reads the member list), or another member. `null` when the record names
 * nobody.
 */
export function uploader(
  uploadedBy: string | null,
  selfId: string | null,
  names: ReadonlyMap<string, string>
): Uploader | null {
  if (!uploadedBy) return null;
  if (selfId && uploadedBy === selfId) return { kind: 'you' };
  const name = names.get(uploadedBy);
  return name ? { kind: 'member', name } : { kind: 'other' };
}
