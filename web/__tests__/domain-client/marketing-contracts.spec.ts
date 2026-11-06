import {
  RESPONSE_VALIDATORS,
  REQUEST_VALIDATORS,
} from '@/lib/domain-client/generated/validators';
import { DefaultDomainGateway } from '@/lib/domain-client/gateway';
import { CsrfTokenManager } from '@/lib/domain-client/csrf';
import { IntentTracker } from '@/lib/domain-client/intent';
import fixture from '../fixtures/marketing-workflow.json';

type PlanFixture = {
  angles: Array<{ expressions: Array<Record<string, unknown>> }>;
};

function withExpressionDetails<T extends PlanFixture>(plan: T) {
  return {
    ...plan,
    plan_review: [],
    guiding_questions: [],
    angles: plan.angles.map((angle) => ({
      ...angle,
      expressions: angle.expressions.map((expression, index) => ({
        ...expression,
        how_to_tell: 'Show one real use step by step.',
        visual_mechanism: index ? 'scenario_story' : 'live_connection_demo',
        evidence_basis: { fact_ids: [], gap: 'Confirm before production.' },
      })),
    })),
  };
}

test('generated workflow validators reject corrupted nested assets and evaluations', () => {
  const validate = RESPONSE_VALIDATORS.get('get_marketing_workflow_200')!;
  const valid = {
    project_id: fixture.project.id,
    rounds: [],
    current: fixture.evaluated,
    next_action: 'read_results_and_close',
    poll_after_ms: 0,
  };
  expect(validate(valid)).toBe(true);
  const corruptAsset = JSON.parse(JSON.stringify(valid));
  corruptAsset.current.assets[0].ready = 'yes';
  expect(validate(corruptAsset)).toBe(false);
  const corruptMetric = JSON.parse(JSON.stringify(valid));
  corruptMetric.current.evaluation.results[0].totals.spend = { amount: 20 };
  expect(validate(corruptMetric)).toBe(false);
});

test('history gateway preserves scoped cursor, search text and page size', async () => {
  const original = globalThis.fetch;
  const fetchMock = jest.fn().mockResolvedValue(
    new Response(JSON.stringify({ items: [], next_cursor: null }), {
      status: 200,
    })
  );
  globalThis.fetch = fetchMock;
  try {
    const gateway = new DefaultDomainGateway({
      csrfTokenManager: new CsrfTokenManager({ ttlMs: 1000 }),
      intentTracker: new IntentTracker({
        storage: {
          length: 0,
          clear() {},
          getItem: () => null,
          key: () => null,
          removeItem() {},
          setItem() {},
        },
      }),
    });
    await gateway.listMarketingProjects({
      workspaceSlug: 'alpha',
      limit: 20,
      cursor: 'a/b+=',
      q: 'charger & cable',
    });
    const url = new URL(fetchMock.mock.calls[0][0], 'http://localhost');
    expect(url.pathname).toBe('/api/domain/v1/w/alpha/marketing/projects');
    expect(Object.fromEntries(url.searchParams)).toEqual({
      limit: '20',
      cursor: 'a/b+=',
      q: 'charger & cable',
    });
  } finally {
    globalThis.fetch = original;
  }
});

test('new read validators accept legacy plans while new writes require an explicit advertising language', () => {
  const legacy = JSON.parse(JSON.stringify(fixture.project));
  delete legacy.language_support;
  for (const key of [
    'ad_language',
    'strategy_language',
    'language_confirmation_required',
  ])
    delete legacy.plan[key];
  for (const combo of legacy.plan.combinations) delete combo.image_brief;
  const validate = RESPONSE_VALIDATORS.get('get_marketing_project_200')!;
  expect(validate(legacy)).toBe(true);
  expect(
    REQUEST_VALIDATORS.edit_marketing_project({
      command: 'confirm_plan',
      plan: legacy.plan,
    })
  ).toBe(false);
  const current = {
    ...legacy,
    language_support: 'v1',
    plan: withExpressionDetails({
      ...legacy.plan,
      ad_language: 'zh-CN',
      strategy_language: 'zh-CN',
    }),
  };
  expect(validate(current)).toBe(true);
  expect(
    REQUEST_VALIDATORS.edit_marketing_project({
      command: 'confirm_plan',
      plan: current.plan,
    })
  ).toBe(true);
});

test('read validators keep plans without expression details while writes require them', () => {
  const validate = RESPONSE_VALIDATORS.get('get_marketing_project_200')!;
  const legacy = JSON.parse(JSON.stringify(fixture.project));
  expect(validate(legacy)).toBe(true);
  expect(
    REQUEST_VALIDATORS.edit_marketing_project({
      command: 'save_plan',
      plan: legacy.plan,
    })
  ).toBe(false);
  const current = withExpressionDetails(legacy.plan);
  expect(
    validate({ ...legacy, plan_review_support: 'v1', plan: current })
  ).toBe(true);
  expect(
    REQUEST_VALIDATORS.edit_marketing_project({
      command: 'save_plan',
      plan: current,
    })
  ).toBe(true);
  expect(
    REQUEST_VALIDATORS.edit_marketing_project({
      command: 'save_plan',
      plan: {
        ...current,
        guiding_questions: [1, 2, 3, 4].map((n) => ({
          id: `q${n}`,
          question: 'Which offer?',
          options: ['A', 'B'],
          why_needed: 'Changes the goal.',
        })),
      },
    })
  ).toBe(false);
});
