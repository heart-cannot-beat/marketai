import {
  audienceCardsFromContent,
  buildCreatePayload,
  buildDirectionAdd,
  buildDirectionEdit,
  defaultAnswers,
  directionImages,
  draftFromDirection,
  formatCost,
  groupItemsByDirection,
  itemDimensions,
  planNeedsPolling,
  planPhase,
  planSignature,
  problemKey,
  ruleOfThumbImages,
  type DirectionView,
  type PlanRunItem,
  type TestPlanDetail,
} from '@/lib/test-plans/plan-model';
import { sanitizeTestPlanEvent } from '@/lib/test-plans/telemetry';

function direction(overrides: Partial<DirectionView> = {}): DirectionView {
  return {
    direction_id: 'dir-1',
    ordinal: 0,
    title: '雨夜车库',
    what_it_tests: '夜间场景是否更打动通勤族',
    hypothesis: '',
    scene_description: '雨夜车库里一键预约',
    copy: { headline: '谷电自动充', primary_text: '下班插上就走' },
    factors: [],
    factor_values: { scene: ['night'], person: ['with', 'without'] },
    combination_count: 2,
    combination_signatures: [
      'direction=dir-1|person=with|scene=night',
      'direction=dir-1|person=without|scene=night',
    ],
    selected_in: { sample: 1, small: 2, full: 2 },
    removed: false,
    user_added: false,
    ...overrides,
  };
}

function plan(overrides: Partial<TestPlanDetail> = {}): TestPlanDetail {
  return {
    plan_id: 'atp_1',
    status: 'ready',
    revision: 3,
    product_id: 'prod_1',
    product_entry_id: 'lib_1',
    product_version_id: 'libver_1',
    product_name: 'VoltNest',
    goal: 'explore',
    audience: { text: '新车主' },
    scale: 'sample',
    defaults: {
      provider: 'mock',
      model: 'mock-v1',
      execution_mode: 'mock',
      aspect_ratio: '1:1',
      variants: { sample: 1, small: 2, full: 2 },
      source: 'computed',
      revision: 0,
      credential_bound: false,
    },
    runs: [],
    replayed: false,
    ...overrides,
  } as TestPlanDetail;
}

describe('three questions -> create payload', () => {
  it('defaults: explore, first audience card, sample, no seeds', () => {
    expect(buildCreatePayload('prod_1', defaultAnswers(2))).toEqual({
      product_id: 'prod_1',
      goal: 'explore',
      scale: 'sample',
      audience_card_index: 0,
      seed_asset_version_ids: [],
    });
  });

  it('a product without audience cards leaves the audience to the server', () => {
    const body = buildCreatePayload('prod_1', defaultAnswers(0));
    expect(body).not.toHaveProperty('audience_card_index');
    expect(body).not.toHaveProperty('audience_text');
  });

  it('free text replaces the card; seeds travel only with optimize', () => {
    expect(
      buildCreatePayload('prod_1', {
        goal: 'optimize',
        audienceCardIndex: null,
        audienceText: '  刚提车的上班族 ',
        scale: 'small',
        seedAssetVersionIds: ['av_1', 'av_2'],
      })
    ).toEqual({
      product_id: 'prod_1',
      goal: 'optimize',
      scale: 'small',
      audience_text: '刚提车的上班族',
      seed_asset_version_ids: ['av_1', 'av_2'],
    });
    expect(
      buildCreatePayload('prod_1', {
        goal: 'explore',
        audienceCardIndex: 1,
        audienceText: '',
        scale: 'full',
        seedAssetVersionIds: ['av_1'],
      }).seed_asset_version_ids
    ).toEqual([]);
  });

  it('reads audience cards from the published product content', () => {
    expect(
      audienceCardsFromContent({
        audiences: [{ who: '新车主', why: '省电费' }, { who: ' ' }, 'x'],
      })
    ).toEqual([{ who: '新车主', why: '省电费', pain: '' }]);
    expect(audienceCardsFromContent(undefined)).toEqual([]);
  });

  it('rule of thumb before drafting uses the defaults variants', () => {
    expect(ruleOfThumbImages('small', { variants: { small: 3 } })).toEqual({
      maxCombinations: 12,
      variants: 3,
      maxImages: 36,
    });
    expect(ruleOfThumbImages('full', undefined).maxImages).toBe(64);
    expect(ruleOfThumbImages('sample', undefined).maxImages).toBeNull();
  });
});

describe('revise payloads', () => {
  it('sends only the fields that changed', () => {
    const view = direction();
    const draft = draftFromDirection(view);
    expect(buildDirectionEdit(view, draft)).toBeNull();

    draft.whatItTests = '夜景 vs 白天';
    draft.headline = '谷电自动充，省一半';
    draft.factorValues.person = ['with'];
    expect(buildDirectionEdit(view, draft)).toEqual({
      direction_id: 'dir-1',
      what_it_tests: '夜景 vs 白天',
      copy: { headline: '谷电自动充，省一半' },
      factor_values: { person: [{ value_id: 'with' }] },
    });
  });

  it('a new idea needs a title', () => {
    expect(buildDirectionAdd({ title: ' ', sceneDescription: 'x' })).toBeNull();
    expect(
      buildDirectionAdd({ title: '露营补电', sceneDescription: '' })
    ).toEqual({ title: '露营补电' });
    expect(
      buildDirectionAdd({ title: '露营补电', sceneDescription: '营地夜景' })
    ).toEqual({ title: '露营补电', scene_description: '营地夜景' });
  });
});

describe('run items -> direction cards', () => {
  const directions = [
    direction(),
    direction({
      direction_id: 'dir-2',
      combination_signatures: ['direction=dir-2|person=with|scene=day'],
    }),
  ];

  function item(overrides: Partial<PlanRunItem>): PlanRunItem {
    return { id: 'ri', ordinal: 0, state: 'succeeded', ...overrides };
  }

  it('builds the plan signature from tags, factors sorted', () => {
    expect(
      planSignature({ scene: 'night', direction: 'dir-1', person: 'with' })
    ).toBe('direction=dir-1|person=with|scene=night');
    expect(planSignature({ scene: 'night' })).toBeNull();
  });

  it('falls back to the fingerprint and to full template keys', () => {
    expect(
      itemDimensions(
        item({
          combination_fingerprint:
            'lib_t:direction=dir-2:lib_t:person=with:lib_t:scene=day',
        })
      )
    ).toEqual({ direction: 'dir-2', person: 'with', scene: 'day' });
    expect(
      itemDimensions(
        item({ tags: { 'lib_t:direction': 'dir-1', 'lib_t:scene': 'night' } })
      )
    ).toEqual({ direction: 'dir-1', scene: 'night' });
  });

  it('groups by signature, then by direction, and keeps strays apart', () => {
    const items = [
      item({
        id: 'a',
        ordinal: 2,
        tags: { direction: 'dir-2', person: 'with', scene: 'day' },
      }),
      item({
        id: 'b',
        ordinal: 0,
        tags: { direction: 'dir-1', person: 'with', scene: 'night' },
      }),
      // Factor value edited after the Run: still dir-1's image.
      item({
        id: 'c',
        ordinal: 1,
        tags: { direction: 'dir-1', person: 'kid', scene: 'night' },
      }),
      item({ id: 'd', ordinal: 3, tags: { direction: 'dir-9' } }),
    ];
    const grouped = groupItemsByDirection(directions, items);
    expect(grouped.byDirection.get('dir-1')?.map((i) => i.id)).toEqual([
      'b',
      'c',
    ]);
    expect(grouped.byDirection.get('dir-2')?.map((i) => i.id)).toEqual(['a']);
    expect(grouped.unmatched.map((i) => i.id)).toEqual(['d']);
  });
});

describe('stages and phases', () => {
  it('walks drafting -> review -> sampling -> sampled -> expanding -> done', () => {
    expect(planPhase(plan({ status: 'drafting' }))).toBe('drafting');
    expect(planPhase(plan({ status: 'failed' }))).toBe('failed');
    expect(planPhase(plan())).toBe('review');
    const sample = {
      stage: 'sample',
      target_scale: 'sample',
      status: 'submitted',
      plan_revision: 3,
      run_id: 'gr_1',
      run_status: 'running',
      items_total: 3,
      items_done: 1,
      items_failed: 0,
    };
    expect(planPhase(plan({ runs: [sample] }))).toBe('sampling');
    expect(planNeedsPolling(plan({ runs: [sample] }))).toBe(true);
    const settled = { ...sample, run_status: 'succeeded', items_done: 3 };
    expect(planPhase(plan({ runs: [settled] }))).toBe('sampled');
    expect(planNeedsPolling(plan({ runs: [settled] }))).toBe(false);
    // A failed materialisation is retried by running the stage again.
    expect(
      planPhase(plan({ runs: [{ ...sample, status: 'failed', run_id: null }] }))
    ).toBe('review');
    const full = {
      ...sample,
      stage: 'full',
      target_scale: 'full',
      status: 'materializing',
      run_id: null,
      run_status: null,
    };
    expect(planPhase(plan({ runs: [settled, full] }))).toBe('expanding');
    expect(
      planPhase(
        plan({
          runs: [
            settled,
            {
              ...full,
              status: 'submitted',
              run_id: 'gr_2',
              run_status: 'partially_succeeded',
            },
          ],
        })
      )
    ).toBe('done');
  });

  it('counts a direction images per stage and formats cost', () => {
    const stage = {
      scale: 'small',
      combinations: 6,
      variants: 2,
      images: 12,
      max_cost: { amount: '0.5', currency: 'USD' },
      untested: {
        count: 0,
        space_count: 6,
        constraint_excluded_count: 0,
        summary: '',
      },
    };
    expect(directionImages(direction(), stage)).toBe(4);
    expect(directionImages(direction({ removed: true }), stage)).toBe(0);
    expect(formatCost({ amount: '0', currency: 'USD' }, 'en')).toEqual({
      free: true,
      text: '',
    });
    const paid = formatCost({ amount: '0.32', currency: 'USD' }, 'en');
    expect(paid.free).toBe(false);
    expect(paid.text).toBe('$0.32');
    // The amount follows the interface language, not a fixed zh-CN format.
    expect(formatCost({ amount: '0.32', currency: 'USD' }, 'zh-CN').text).toBe(
      'US$0.32'
    );
  });

  it('maps known problem codes to copy, the rest to a generic line', () => {
    expect(problemKey('revision_conflict')).toBe('problem-revision_conflict');
    expect(problemKey('something_odd')).toBe('problem-unknown');
    expect(problemKey(undefined)).toBe('problem-unknown');
  });
});

describe('telemetry', () => {
  it('keeps bounded labels only', () => {
    expect(
      sanitizeTestPlanEvent({
        event: 'test_plan_revised',
        changes: ['edit', 'remove', 'edit', 'dir-1' as any],
        failureCode: 'Direction dir-1 not found',
        goal: 'atp_1',
      })
    ).toEqual({
      event: 'test_plan_revised',
      changes: ['edit', 'remove'],
      failureCode: 'unknown',
      goal: 'unknown',
    });
    expect(sanitizeTestPlanEvent({ event: 'nope' as any })).toBeNull();
  });
});
