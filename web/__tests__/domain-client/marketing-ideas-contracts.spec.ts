/**
 * Ads-first ideas (ADR 0018, #189-#191): the generated validators and the gateway methods.
 */
import {
  RESPONSE_VALIDATORS,
  REQUEST_VALIDATORS,
} from '@/lib/domain-client/generated/validators';
import { DefaultDomainGateway } from '@/lib/domain-client/gateway';
import { CsrfTokenManager } from '@/lib/domain-client/csrf';
import { IntentTracker } from '@/lib/domain-client/intent';

const detail = {
  id: 'mp_1',
  revision: 1,
  state: 'input',
  product_id: 'prod_1',
  market: 'United States',
  ad_language: 'en-US',
  understanding: null,
  ideas: null,
  latest_call: null,
  ads: [],
  pending: [],
};

function makeGateway(fetchMock: jest.Mock) {
  const storage = new Map<string, string>();
  return new DefaultDomainGateway({
    csrfTokenManager: new CsrfTokenManager({
      ttlMs: 60_000,
      fetchFn: async () => ({
        ok: true,
        status: 200,
        json: async () => ({ token: 'csrf_1' }),
      }),
    }),
    intentTracker: new IntentTracker({
      storage: {
        get length() {
          return storage.size;
        },
        clear: () => storage.clear(),
        getItem: (key: string) => storage.get(key) ?? null,
        key: (index: number) => Array.from(storage.keys())[index] ?? null,
        removeItem: (key: string) => void storage.delete(key),
        setItem: (key: string, value: string) => void storage.set(key, value),
      },
    }),
    fetchFn: fetchMock as unknown as typeof fetch,
  });
}

function reply(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function call(fetchMock: jest.Mock) {
  const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit?];
  const url = new URL(path, 'http://localhost');
  return {
    method: init?.method ?? 'GET',
    pathname: url.pathname,
    query: Object.fromEntries(url.searchParams),
    body: init?.body ? JSON.parse(String(init.body)) : undefined,
  };
}

test('an ads-first detail has only the three ads-first states', () => {
  const validate = RESPONSE_VALIDATORS.get('get_marketing_ideas_200')!;
  expect(validate(detail)).toBe(true);
  expect(validate({ ...detail, state: 'plan_confirmed' })).toBe(false);
  expect(validate({ ...detail, plan: null })).toBe(false);
});

test('a kept ad is a draft in step 1 and carries its combination', () => {
  const validate = RESPONSE_VALIDATORS.get('set_marketing_ad_kept_200')!;
  const ad = {
    asset_version_id: 'av_1',
    combination_id: 'c1',
    angle_id: 'a1',
    expression_id: 'e1',
    status: 'draft',
    kept: true,
    created_at: '2026-11-16T00:00:00+00:00',
  };
  expect(validate({ ...detail, ads: [ad] })).toBe(true);
  expect(validate({ ...detail, ads: [{ ...ad, status: 'approved' }] })).toBe(
    false
  );
});

test('creating ideas needs a product, the market and an advertising language tag', () => {
  const create = REQUEST_VALIDATORS.create_marketing_ideas;
  const body = {
    product_id: 'prod_1',
    source_text: 'Schedule charging from the Acme Charge app.',
    market: 'United States',
    ad_language: 'en-US',
  };
  expect(create(body)).toBe(true);
  expect(create({ ...body, product_id: undefined })).toBe(false);
  expect(create({ ...body, ad_language: 'English' })).toBe(false);
  expect(REQUEST_VALIDATORS.set_marketing_ad_kept({ kept: true })).toBe(true);
  expect(REQUEST_VALIDATORS.set_marketing_ad_kept({})).toBe(false);
});

test('drafted ideas carry an ideas call; a plan call is not one of theirs (#190)', () => {
  const validate = RESPONSE_VALIDATORS.get('get_marketing_ideas_200')!;
  const latest = {
    id: 'pc_1',
    operation_id: 'op_1',
    kind: 'ideas',
    state: 'succeeded',
    execution_mode: 'byok',
    error_code: null,
    maximum_cost: '0.10',
  };
  expect(
    validate({ ...detail, state: 'ideas_ready', latest_call: latest })
  ).toBe(true);
  expect(
    validate({ ...detail, latest_call: { ...latest, kind: 'plan' } })
  ).toBe(false);
  const accepted = RESPONSE_VALIDATORS.get(
    'start_marketing_ideas_planning_202'
  )!;
  expect(accepted({ ...latest, state: 'queued' })).toBe(true);
  expect(accepted({ ...latest, kind: 'plan' })).toBe(false);
});

test('drafting asks for the understanding or the ideas only', () => {
  const start = REQUEST_VALIDATORS.start_marketing_ideas_planning;
  expect(
    start({ kind: 'ideas', execution_mode: 'byok', quote_hash: 'q' })
  ).toBe(true);
  expect(start({ kind: 'understanding', execution_mode: 'mock' })).toBe(true);
  expect(start({ kind: 'plan', execution_mode: 'byok' })).toBe(false);
  expect(start({ kind: 'ideas' })).toBe(false);
});

test('preparing images takes the image settings and answers one draft per ad (#191)', () => {
  const prepare = REQUEST_VALIDATORS.prepare_marketing_ideas_generation;
  expect(prepare({ provider: 'openai', model: 'gpt-image-1' })).toBe(true);
  expect(
    prepare({
      provider: 'mock',
      model: 'mock-v1',
      aspect_ratio: '1:1',
      variants: 2,
    })
  ).toBe(true);
  expect(
    prepare({ provider: 'openai', model: 'gpt-image-1', variants: 5 })
  ).toBe(false);
  expect(
    prepare({
      provider: 'openai',
      model: 'gpt-image-1',
      product_references: {},
    })
  ).toBe(false);
  const prepared = RESPONSE_VALIDATORS.get(
    'prepare_marketing_ideas_generation_201'
  )!;
  const drafts = {
    project_id: 'mp_1',
    revision: 5,
    drafts: [{ combination_id: 'c1_1', draft_id: 'gd_1' }],
  };
  expect(prepared(drafts)).toBe(true);
  expect(prepared({ ...drafts, drafts: [{ combination_id: 'c1_1' }] })).toBe(
    false
  );
});

test('an image still being made is pending with its ad', () => {
  const validate = RESPONSE_VALIDATORS.get('get_marketing_ideas_200')!;
  const pending = [{ combination_id: 'c1_1', state: 'running' }];
  expect(validate({ ...detail, state: 'ideas_ready', pending })).toBe(true);
  expect(
    validate({
      ...detail,
      pending: [{ combination_id: 'c1_1', state: 'succeeded' }],
    })
  ).toBe(false);
});

describe('ideas gateway', () => {
  test('prepares images with the revision as If-Match', async () => {
    const answer = {
      project_id: 'mp_1',
      revision: 5,
      drafts: [{ combination_id: 'c1_1', draft_id: 'gd_1' }],
    };
    const fetchMock = jest.fn().mockResolvedValue(reply(201, answer));
    const body = {
      provider: 'openai' as const,
      model: 'gpt-image-1',
      aspect_ratio: '4:5' as const,
      image_size: '1K' as const,
      variants: 1,
    };
    const result = await makeGateway(fetchMock).prepareMarketingIdeasGeneration(
      {
        workspaceSlug: 'acme',
        actionInstanceId: 'ideas:prepare:1',
        expectedRevision: 5,
        projectId: 'mp_1',
        body,
      }
    );
    expect(result).toMatchObject({ kind: 'created' });
    expect(call(fetchMock)).toEqual({
      method: 'POST',
      pathname: '/api/domain/v1/w/acme/marketing/ideas/mp_1/generation-drafts',
      query: {},
      body,
    });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)['If-Match']).toBe('5');
  });

  test('starts drafting with the revision as If-Match', async () => {
    const fetchMock = jest.fn().mockResolvedValue(
      reply(202, {
        id: 'pc_1',
        operation_id: 'op_1',
        kind: 'ideas',
        state: 'queued',
        execution_mode: 'byok',
        error_code: null,
        maximum_cost: '0.10',
      })
    );
    const body = {
      kind: 'ideas' as const,
      execution_mode: 'byok' as const,
      strategy_language: 'en-US' as const,
      quote_hash: 'q',
    };
    const result = await makeGateway(fetchMock).startMarketingIdeasPlanning({
      workspaceSlug: 'acme',
      actionInstanceId: 'ideas:plan:1',
      expectedRevision: 3,
      projectId: 'mp_1',
      body,
    });
    expect(result).toMatchObject({ kind: 'accepted' });
    expect(call(fetchMock)).toEqual({
      method: 'POST',
      pathname: '/api/domain/v1/w/acme/marketing/ideas/mp_1/planning',
      query: {},
      body,
    });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)['If-Match']).toBe('3');
  });

  test('lists ideas with the page size and cursor', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValue(reply(200, { items: [], next_cursor: null }));
    await makeGateway(fetchMock).listMarketingIdeas({
      workspaceSlug: 'acme',
      limit: 20,
      cursor: 'a/b+=',
    });
    expect(call(fetchMock)).toEqual({
      method: 'GET',
      pathname: '/api/domain/v1/w/acme/marketing/ideas',
      query: { limit: '20', cursor: 'a/b+=' },
      body: undefined,
    });
  });

  test('reads one project of ideas', async () => {
    const fetchMock = jest.fn().mockResolvedValue(reply(200, detail));
    await makeGateway(fetchMock).getMarketingIdeas({
      workspaceSlug: 'acme',
      projectId: 'mp_1',
    });
    expect(call(fetchMock)).toMatchObject({
      method: 'GET',
      pathname: '/api/domain/v1/w/acme/marketing/ideas/mp_1',
    });
  });

  test('creates ideas for a product', async () => {
    const fetchMock = jest.fn().mockResolvedValue(reply(201, detail));
    const body = {
      product_id: 'prod_1',
      source_text: 'Schedule charging from the Acme Charge app.',
      market: 'United States',
      ad_language: 'en-US',
    };
    const result = await makeGateway(fetchMock).createMarketingIdeas({
      workspaceSlug: 'acme',
      actionInstanceId: 'ideas:create:1',
      body,
    });
    expect(result).toMatchObject({ kind: 'created' });
    expect(call(fetchMock)).toEqual({
      method: 'POST',
      pathname: '/api/domain/v1/w/acme/marketing/ideas',
      query: {},
      body,
    });
  });

  test('keeps an ad', async () => {
    const fetchMock = jest.fn().mockResolvedValue(reply(200, detail));
    const result = await makeGateway(fetchMock).setMarketingAdKept({
      workspaceSlug: 'acme',
      actionInstanceId: 'ideas:keep:av_1',
      projectId: 'mp_1',
      assetVersionId: 'av_1',
      body: { kept: true },
    });
    expect(result).toMatchObject({ kind: 'ok' });
    expect(call(fetchMock)).toEqual({
      method: 'PUT',
      pathname: '/api/domain/v1/w/acme/marketing/ideas/mp_1/ads/av_1/kept',
      query: {},
      body: { kept: true },
    });
  });
});
