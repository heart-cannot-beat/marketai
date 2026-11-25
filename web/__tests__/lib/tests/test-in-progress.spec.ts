import {
  affectedComparisons,
  bindingChanges,
  checklist,
  initialChoices,
  matchedCount,
  startDateQuestion,
  type BindingSuggestions,
  type TestComparison,
  type TestReport,
  type TestResult,
  type TestSchedule,
} from '@/lib/tests/test-in-progress';
import type { TestRound } from '@/lib/tests/take-to-test';

/** How the "test in progress" screen (#257) turns choices into commands; every number stays the server's. */

const entry = (id: string, version: string) => ({
  entry_id: id,
  ad_name: `MA-7Q2 · ${id}`,
  display_name: null,
  asset_version_id: version,
  asset_code: `MK-${id}`,
});

function suggestions(
  ads: Partial<BindingSuggestions['ads'][number]>[],
  locked = false
): BindingSuggestions {
  return {
    round_id: 'mr_1',
    batch_id: 'mmb_1',
    ad_account_id: '1234',
    ad_name_column: 'Ad name',
    locked,
    entries: [entry('e1', 'av_1'), entry('e3', 'av_3')],
    ads: ads.map((ad, i) => ({
      ad_id: String(100 + i),
      ad_names: [`Meta ad ${i}`],
      status: 'no_match',
      entry_id: null,
      key_kind: null,
      candidate_entry_ids: [],
      other_round_ids: [],
      ...ad,
    })),
  } as BindingSuggestions;
}

describe('binding choices (#257)', () => {
  it('starts from the current match or the suggestion; a skipped ad without one is left out', () => {
    const s = suggestions([
      { status: 'suggested', entry_id: 'e1', key_kind: 'ma_key' },
      { status: 'bound', entry_id: 'e3' },
      { status: 'no_match' },
    ]);
    const choices = initialChoices(s);
    expect(choices).toEqual({ '100': 'e1', '101': 'e3', '102': '' });
    // The suggestion is new; the match the round has changes nothing.
    expect(bindingChanges(s, choices)).toEqual([
      { ad_id: '100', entry_id: 'e1' },
    ]);
    expect(matchedCount(s, choices)).toBe(2);
  });

  it('takes a match away only while no report counts', () => {
    const s = suggestions([{ status: 'bound', entry_id: 'e3' }]);
    expect(bindingChanges(s, { '100': '' })).toEqual([
      { ad_id: '100', entry_id: null },
    ]);
    const locked = suggestions([{ status: 'bound', entry_id: 'e3' }], true);
    expect(bindingChanges(locked, { '100': '' })).toEqual([]);
    expect(matchedCount(locked, { '100': '' })).toBe(1);
  });
});

describe('the first report’s start date (#257)', () => {
  const schedule = (extra: Partial<TestSchedule> = {}) =>
    ({
      effective_start_date: '2026-11-26',
      start_movable: true,
      ...extra,
    }) as TestSchedule;
  const report = (first: string | null) =>
    ({ first_date: first }) as TestReport;

  it('asks when the report’s first day is not the start, while the start can move', () => {
    expect(startDateQuestion(report('2026-11-28'), schedule())).toBe(
      '2026-11-28'
    );
    expect(startDateQuestion(report('2026-11-26'), schedule())).toBeNull();
    expect(
      startDateQuestion(
        report('2026-11-28'),
        schedule({ start_movable: false })
      )
    ).toBeNull();
    expect(startDateQuestion(report(null), schedule())).toBeNull();
    expect(startDateQuestion(null, schedule())).toBeNull();
  });
});

describe('a deviation’s groups (#257)', () => {
  it('names every compared group holding a picked ad', () => {
    const groups = [
      { id: 'g1', label: 'Overnight', asset_version_ids: ['av_1'] },
      { id: 'g2', label: 'Outdoors', asset_version_ids: ['av_3', 'av_4'] },
    ] as TestComparison[];
    expect(
      affectedComparisons(groups, ['av_4']).map((group) => group.id)
    ).toEqual(['g2']);
    expect(affectedComparisons(groups, [])).toEqual([]);
  });
});

describe('the three steps (#257)', () => {
  const round = (extra: Partial<TestRound> = {}) =>
    ({ bindings: [], batches: [], ...extra }) as unknown as TestRound;
  const result = (extra: Partial<TestResult> = {}) =>
    ({
      mature: false,
      evaluation_id: null,
      current: false,
      ...extra,
    }) as TestResult;

  it('follows what the server has: matches, a counted report, a current result', () => {
    expect(checklist(round(), result())).toEqual({
      active: 'run',
      done: { run: false, report: false, result: false },
    });
    expect(
      checklist(round({ bindings: [{ id: 'b' }] } as never), result()).active
    ).toBe('report');
    const counted = round({ batches: [{ state: 'confirmed' }] } as never);
    expect(checklist(counted, result())).toEqual({
      active: 'report',
      done: { run: true, report: true, result: false },
    });
    expect(checklist(counted, result({ mature: true })).active).toBe('result');
    expect(
      checklist(
        counted,
        result({ mature: true, evaluation_id: 'ev', current: true })
      ).active
    ).toBeNull();
  });
});
