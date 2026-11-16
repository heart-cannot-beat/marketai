/**
 * Ads-first ideas (ADR 0018, #189): the generated validators and the four gateway methods.
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

describe('ideas gateway', () => {
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
