/**
 * Quoting a test of the kept ads (#248): the generated validator, the proxy route and query rules, and the
 * gateway method. The product is the fictional Acme Charge Home.
 */
import { RESPONSE_VALIDATORS } from '@/lib/domain-client/generated/validators';
import { matchAllowlistEntry } from '@/lib/domain-client/request-pipeline';
import { DefaultDomainGateway } from '@/lib/domain-client/gateway';
import { CsrfTokenManager } from '@/lib/domain-client/csrf';
import { IntentTracker } from '@/lib/domain-client/intent';

const quote = {
  available: true,
  unavailable_reason: null,
  plan_error: null,
  not_exportable: [],
  ideas_revision: 7,
  execution_mode: 'byok',
  comparison: 'claims',
  question:
    'Which of these 2 buying claims brings orders at a cost per order of 10.00 USD or less?',
  subjects: [
    {
      kind: 'claim',
      angle_id: 'a1',
      expression_id: null,
      title: 'Charge overnight from the app',
      combination_ids: ['c1_1'],
      asset_version_ids: ['av_1'],
      budget: '200.00',
      expected_orders: 20,
    },
    {
      kind: 'claim',
      angle_id: 'a2',
      expression_id: null,
      title: 'The circuit never trips',
      combination_ids: ['c2_1', 'c2_2'],
      asset_version_ids: ['av_2', 'av_3'],
      budget: '200.00',
      expected_orders: 20,
    },
  ],
  target_cost: '10.00',
  round_budget: '400.00',
  currency: 'USD',
  duration_days: 7,
  min_purchases: 20,
  tier: 'enough',
  needed_budget: '400.00',
  suggested_subjects: null,
  start_date: '2026-12-01',
  end_date: '2026-12-07',
  conclusion_date: '2026-12-16',
  account_timezone: 'America/New_York',
  attribution_window: '7d_click_1d_view',
  attribution_lag_days: 8,
  landing_page: 'https://acmecharge.example/products/home',
  landing_page_source: 'product_link',
  checks: {
    available: true,
    unavailable_reason: null,
    quantity: 3,
    maximum_cost: '0.15',
    currency: 'USD',
  },
  quote_hash: 'a'.repeat(64),
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

test('the quote validates, and unknown values or fields do not', () => {
  const validate = RESPONSE_VALIDATORS.get('get_marketing_test_quote_200')!;
  expect(validate(quote)).toBe(true);
  const refused = {
    ...quote,
    available: false,
    unavailable_reason: 'ads_not_exportable',
    not_exportable: [
      {
        asset_version_id: 'av_2',
        status: 'draft',
        reasons: [
          { code: 'facts_unverified', fact_ids: ['f2'], finding_ids: [] },
        ],
      },
    ],
  };
  expect(validate(refused)).toBe(true);
  expect(validate({ ...quote, tier: 'plenty' })).toBe(false);
  expect(validate({ ...quote, unavailable_reason: 'too_expensive' })).toBe(
    false
  );
  expect(validate({ ...quote, extra: true })).toBe(false);
});

test('the proxy reads the quote route and refuses answers outside the rules', () => {
  const matched = matchAllowlistEntry('GET', [
    'marketing',
    'ideas',
    'mp_1',
    'test-quote',
  ]);
  expect(matched?.entry.operationId).toBe('get_marketing_test_quote');
  expect(matched?.extractedParams).toEqual({ project_id: 'mp_1' });
  const query = matched!.entry.querySchema!;
  const answers = {
    target_cost: '10',
    round_budget: '400',
    duration_days: '7',
  };
  expect(query.safeParse(answers).success).toBe(true);
  expect(
    query.safeParse({ ...answers, landing_page: 'https://acmecharge.example' })
      .success
  ).toBe(true);
  expect(query.safeParse({ ...answers, duration_days: '6' }).success).toBe(
    false
  );
  expect(
    query.safeParse({ round_budget: '400', duration_days: '7' }).success
  ).toBe(false);
  expect(query.safeParse({ ...answers, something: 'else' }).success).toBe(
    false
  );
});

test('the gateway sends the answers as the query', async () => {
  const fetchMock = jest.fn().mockResolvedValue(
    new Response(JSON.stringify(quote), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  );
  const result = await makeGateway(fetchMock).getMarketingTestQuote({
    workspaceSlug: 'acme',
    projectId: 'mp_1',
    query: {
      target_cost: '10',
      round_budget: '400',
      duration_days: '7',
      account_timezone: 'America/New_York',
      landing_page: undefined,
    },
  });
  expect(result).toMatchObject({ kind: 'ok' });
  const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit?];
  const url = new URL(path, 'http://localhost');
  expect(init?.method ?? 'GET').toBe('GET');
  expect(url.pathname).toBe(
    '/api/domain/v1/w/acme/marketing/ideas/mp_1/test-quote'
  );
  expect(Object.fromEntries(url.searchParams)).toEqual({
    target_cost: '10',
    round_budget: '400',
    duration_days: '7',
    account_timezone: 'America/New_York',
  });
});
