/**
 * The ads-first first batch (#193): the generated validators and the gateway methods.
 */
import {
  RESPONSE_VALIDATORS,
  REQUEST_VALIDATORS,
} from '@/lib/domain-client/generated/validators';
import { DOMAIN_ALLOWLIST } from '@/lib/domain-client/generated/allowlist';
import { matchAllowlistEntry } from '@/lib/domain-client/request-pipeline';
import { DefaultDomainGateway } from '@/lib/domain-client/gateway';
import { CsrfTokenManager } from '@/lib/domain-client/csrf';
import { IntentTracker } from '@/lib/domain-client/intent';

const batch = {
  id: 'mfb_1',
  operation_id: 'op_1',
  state: 'needs_attention',
  needs_attention: true,
  attention: {
    reason: 'too_few_claims',
    step: 'ideas',
    variants: [],
    combination_ids: [],
    resumable: true,
  },
  resume: {
    maximum_cost: '0.85',
    additional_cost: '0.2',
    currency: 'USD',
    quote_hash: 'q2',
    requires: ['additional_text'],
  },
  intake_id: 'pin_1',
  product_id: 'prod_1',
  project_id: 'mp_1',
  market: 'United States',
  ad_language: 'en-US',
  strategy_language: 'en-US',
  execution_mode: 'byok',
  selected_variant: null,
  confirmed_max_cost: '0.65',
  reserved_total: '0.25',
  currency: 'USD',
  image_provider: 'openai',
  image_model: 'gpt-image-1.5',
  ads_planned: null,
  created_at: '2026-11-17T00:00:00+00:00',
  updated_at: '2026-11-17T00:00:00+00:00',
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

test('a stopped first batch says why and what continuing costs', () => {
  const validate = RESPONSE_VALIDATORS.get('get_marketing_first_batch_200')!;
  expect(validate(batch)).toBe(true);
  expect(validate({ ...batch, state: 'paused' })).toBe(false);
  expect(
    validate({ ...batch, attention: { ...batch.attention, reason: 'other' } })
  ).toBe(false);
  expect(
    validate({
      ...batch,
      state: 'done',
      needs_attention: false,
      attention: null,
      resume: null,
      ads_planned: 4,
    })
  ).toBe(true);
});

test('the overview reads a stop per product from the ideas list', () => {
  const validate = RESPONSE_VALIDATORS.get('list_marketing_ideas_200')!;
  const item = {
    id: 'mp_1',
    revision: 5,
    state: 'understanding_draft',
    product_id: 'prod_1',
    product_name: 'Acme Charge Home',
    ad_count: 0,
    kept_count: 0,
    updated_at: '2026-11-17T00:00:00+00:00',
  };
  const stopped = {
    id: 'mfb_1',
    state: 'needs_attention',
    needs_attention: true,
    reason: 'too_few_claims',
  };
  expect(validate({ items: [item] })).toBe(true);
  expect(validate({ items: [{ ...item, first_batch: stopped }] })).toBe(true);
  expect(
    validate({
      items: [{ ...item, first_batch: { ...stopped, reason: 'other' } }],
    })
  ).toBe(false);
});

test('starting needs the market, the language and the confirmed quote', () => {
  const start = REQUEST_VALIDATORS.start_marketing_first_batch;
  const body = {
    url: 'https://acmecharge.example/products/home',
    market: 'United States',
    ad_language: 'en-US',
    quote_hash: 'q1',
  };
  expect(start(body)).toBe(true);
  expect(start({ ...body, quote_hash: undefined })).toBe(false);
  expect(start({ ...body, execution_mode: 'free' })).toBe(false);
  const resume = REQUEST_VALIDATORS.resume_marketing_first_batch;
  expect(
    resume({ quote_hash: 'q2', additional_text: 'Fits a 32 A circuit.' })
  ).toBe(true);
  expect(resume({})).toBe(false);
});

test('the quote is not read as a batch whose id is "quote"', () => {
  const quote = matchAllowlistEntry('GET', ['marketing', 'first-batch-quote']);
  expect(quote?.entry.operationId).toBe('get_marketing_first_batch_quote');
  const read = matchAllowlistEntry('GET', [
    'marketing',
    'first-batch',
    'mfb_1',
  ]);
  expect(read?.entry.operationId).toBe('get_marketing_first_batch');
  expect(read?.extractedParams).toEqual({ first_batch_id: 'mfb_1' });
  expect(
    DOMAIN_ALLOWLIST.filter((entry) =>
      entry.operationId.includes('first_batch')
    ).map((entry) => entry.operationId)
  ).toEqual([
    'get_marketing_first_batch',
    'get_marketing_first_batch_quote',
    'list_marketing_first_batches',
    'resume_marketing_first_batch',
    'start_marketing_first_batch',
  ]);
  // The list (#204) and the start share a path; the method tells them apart.
  expect(
    matchAllowlistEntry('GET', ['marketing', 'first-batch'])?.entry.operationId
  ).toBe('list_marketing_first_batches');
  expect(
    matchAllowlistEntry('POST', ['marketing', 'first-batch'])?.entry.operationId
  ).toBe('start_marketing_first_batch');
});

describe('first batch gateway', () => {
  test('reads the quote for a mode', async () => {
    const answer = {
      available: true,
      unavailable_reason: null,
      execution_mode: 'byok',
      maximum_cost: '0.65',
      currency: 'USD',
      quote_hash: 'q1',
      image_provider: 'openai',
      image_model: 'gpt-image-1.5',
      steps: [
        {
          step: 'reading',
          quantity: 1,
          unit_maximum: '0.05',
          maximum_cost: '0.05',
        },
      ],
    };
    const fetchMock = jest.fn().mockResolvedValue(reply(200, answer));
    const result = await makeGateway(fetchMock).getMarketingFirstBatchQuote({
      workspaceSlug: 'acme',
      executionMode: 'byok',
    });
    expect(result).toMatchObject({ kind: 'ok' });
    expect(call(fetchMock)).toEqual({
      method: 'GET',
      pathname: '/api/domain/v1/w/acme/marketing/first-batch-quote',
      query: { execution_mode: 'byok' },
      body: undefined,
    });
  });

  test('starts and continues a batch', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(reply(201, { ...batch, state: 'reading' }));
    const body = {
      url: 'https://acmecharge.example/products/home',
      market: 'United States',
      ad_language: 'en-US',
      strategy_language: 'en-US' as const,
      execution_mode: 'byok' as const,
      quote_hash: 'q1',
    };
    const started = await makeGateway(fetchMock).startMarketingFirstBatch({
      workspaceSlug: 'acme',
      actionInstanceId: 'first-batch:start:1',
      body,
    });
    expect(started).toMatchObject({ kind: 'created' });
    expect(call(fetchMock)).toEqual({
      method: 'POST',
      pathname: '/api/domain/v1/w/acme/marketing/first-batch',
      query: {},
      body,
    });

    const resumeMock = jest
      .fn()
      .mockResolvedValueOnce(reply(200, { ...batch, state: 'understanding' }));
    const resumed = await makeGateway(resumeMock).resumeMarketingFirstBatch({
      workspaceSlug: 'acme',
      actionInstanceId: 'first-batch:resume:1',
      firstBatchId: 'mfb_1',
      body: { quote_hash: 'q2', additional_text: 'Fits a 32 A circuit.' },
    });
    expect(resumed).toMatchObject({ kind: 'ok' });
    expect(call(resumeMock).pathname).toBe(
      '/api/domain/v1/w/acme/marketing/first-batch/mfb_1/resume'
    );
  });
});
