import {
  checkProgress,
  dayLabel,
  emptyForm,
  formProblems,
  openAds,
  queryOf,
  quoteQuery,
  roundBusy,
  unprovenAds,
  type TakeToTestForm,
  type TestRound,
} from '@/lib/tests/take-to-test';

/**
 * What "take to test" (#256) sends for its answers, and how it reads a
 * started test's round. No number the page shows is computed here.
 */

const form = (extra: Partial<TakeToTestForm> = {}): TakeToTestForm => ({
  ...emptyForm('America/New_York'),
  ...extra,
});

function round(extra: Partial<TestRound> = {}): TestRound {
  return {
    id: 'mr_1',
    revision: 4,
    state: 'reviewing',
    assets: [],
    handoff: null,
    ...extra,
  } as unknown as TestRound;
}

const asset = (id: string, state: string | null, findings: unknown[] = []) =>
  ({
    asset_version_id: id,
    headline: `Headline ${id}`,
    paired_ai_qc: state
      ? { id: `qc_${id}`, state, result: { findings } }
      : null,
  }) as unknown as TestRound['assets'][number];

describe('quoteQuery', () => {
  it('asks nothing until the three answers are given', () => {
    expect(quoteQuery(form())).toBeNull();
    expect(quoteQuery(form({ targetCost: '30' }))).toBeNull();
    expect(quoteQuery(form({ targetCost: '30', roundBudget: '1200' }))).toEqual(
      {
        target_cost: '30',
        round_budget: '1200',
        duration_days: 7,
        account_timezone: 'America/New_York',
      }
    );
  });

  it('reads amounts as people type them, and refuses what the server would', () => {
    expect(
      quoteQuery(form({ targetCost: ' 30.5 ', roundBudget: '1,200' }))
    ).toMatchObject({ target_cost: '30.5', round_budget: '1200' });
    expect(quoteQuery(form({ targetCost: '0', roundBudget: '10' }))).toBeNull();
    expect(
      quoteQuery(form({ targetCost: '1.005', roundBudget: '10' }))
    ).toBeNull();
    expect(
      quoteQuery(
        form({ targetCost: '3', roundBudget: '10', durationDays: '6' })
      )
    ).toBeNull();
    expect(
      quoteQuery(
        form({ targetCost: '3', roundBudget: '10', durationDays: '91' })
      )
    ).toBeNull();
  });

  it('sends a default only once the user changed it', () => {
    const query = quoteQuery(
      form({
        targetCost: '30',
        roundBudget: '600',
        durationDays: '14',
        landingPage: 'https://acmecharge.example/home',
        currency: 'EUR',
        startDate: '2026-12-01',
        attributionWindow: '7d_click',
        maxSmallRounds: '2',
        mediaBudget: '1800',
        audience: '  ',
      })
    );
    expect(query).toEqual({
      target_cost: '30',
      round_budget: '600',
      duration_days: 14,
      landing_page: 'https://acmecharge.example/home',
      currency: 'EUR',
      account_timezone: 'America/New_York',
      start_date: '2026-12-01',
      attribution_window: '7d_click',
      max_small_rounds: 2,
      media_budget: '1800',
    });
    expect(queryOf(query!)).toMatchObject({
      duration_days: '14',
      max_small_rounds: '2',
    });
  });

  it('refuses a landing page that is not a web address', () => {
    expect(
      quoteQuery(
        form({ targetCost: '30', roundBudget: '600', landingPage: 'acme' })
      )
    ).toBeNull();
  });
});

describe('formProblems', () => {
  it('names what is missing or wrong, the landing page only when the product has no link', () => {
    expect(formProblems(form(), false)).toEqual({
      targetCost: 'required',
      roundBudget: 'required',
    });
    expect(
      formProblems(form({ targetCost: '30', roundBudget: '600' }), true)
    ).toEqual({ landingPage: 'required' });
    expect(
      formProblems(
        form({
          targetCost: 'abc',
          roundBudget: '600',
          durationDays: '3',
          maxSmallRounds: '30',
        }),
        false
      )
    ).toEqual({
      targetCost: 'money',
      durationDays: 'days',
      maxSmallRounds: 'rounds',
    });
  });
});

describe('a started round', () => {
  it('counts the "look together" checks that have their result', () => {
    const reading = round({
      assets: [
        asset('av_1', 'succeeded'),
        asset('av_2', 'running'),
        asset('av_3', 'uncertain'),
      ],
    });
    expect(checkProgress(reading)).toEqual({
      total: 3,
      answered: 1,
      allAnswered: false,
    });
    expect(roundBusy(reading)).toBe(true);
    const done = round({
      assets: [asset('av_1', 'succeeded'), asset('av_2', 'failed')],
    });
    expect(checkProgress(done).allAnswered).toBe(true);
    expect(roundBusy(done)).toBe(false);
  });

  it('keeps asking while the package is made, and stops once it is ready or the round closed', () => {
    const handoff = (state: string) =>
      ({ state, entries: [] }) as unknown as TestRound['handoff'];
    expect(roundBusy(round({ handoff: handoff('queued') }))).toBe(true);
    expect(roundBusy(round({ handoff: handoff('ready') }))).toBe(false);
    expect(roundBusy(round({ state: 'closed' }))).toBe(false);
    expect(roundBusy(undefined)).toBe(false);
  });

  it('opens only the ads the refusal names, with their findings in words', () => {
    const findings = [
      {
        check_id: 'comparison_faithful',
        status: 'fail',
        rationale: 'Too alike.',
      },
      { check_id: 'belongs_to_intended_combination', status: 'pass' },
    ];
    const reading = round({
      assets: [
        asset('av_1', 'succeeded', findings),
        asset('av_2', 'succeeded'),
      ],
    });
    const open = openAds(
      {
        findings: [
          {
            asset_version_id: 'av_1',
            check_id: 'qc_av_1',
            finding_ids: ['comparison_faithful'],
          },
        ],
      },
      reading
    );
    expect(open).toEqual([
      {
        assetVersionId: 'av_1',
        checkId: 'qc_av_1',
        headline: 'Headline av_1',
        findings: [findings[0]],
      },
    ]);
    expect(
      unprovenAds(
        { ads: [{ asset_version_id: 'av_2', reason: 'check_failed' }] },
        reading
      )
    ).toEqual([
      {
        assetVersionId: 'av_2',
        headline: 'Headline av_2',
        reason: 'check_failed',
      },
    ]);
  });
});

describe('dayLabel', () => {
  it('writes a calendar day as that day, whatever the browser zone', () => {
    expect(dayLabel('en', '2026-12-02')).toBe('December 2, 2026');
    expect(dayLabel('zh-CN', '2026-12-02')).toBe('2026年12月2日');
  });
});
