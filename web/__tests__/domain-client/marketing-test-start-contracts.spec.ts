/**
 * Starting a test of the kept ads (#249): the generated request and response
 * validators, the BFF allowlist entry and the gateway method. The product is
 * the fictional Acme Charge Home.
 */
import {
  REQUEST_VALIDATORS,
  RESPONSE_VALIDATORS,
} from '@/lib/domain-client/generated/validators';
import { DOMAIN_ALLOWLIST } from '@/lib/domain-client/generated/allowlist';
import { matchAllowlistEntry } from '@/lib/domain-client/request-pipeline';
import { DefaultDomainGateway } from '@/lib/domain-client/gateway';
import { CsrfTokenManager } from '@/lib/domain-client/csrf';
import { IntentTracker } from '@/lib/domain-client/intent';

const start = {
  answers: {
    target_cost: '10',
    round_budget: '400',
    duration_days: 7,
    account_timezone: 'America/New_York',
  },
  quote_hash: 'a'.repeat(64),
  asset_version_ids: ['av_1', 'av_2', 'av_3'],
};

const created = {
  id: 'mp_test',
  revision: 1,
  product_id: 'prod_1',
  source_project_id: 'mp_ideas',
  comparison: 'claims',
  plan_summary: {
    target_cost: '10',
    round_budget: '400',
    duration_days: 7,
    currency: 'USD',
    landing_page: 'https://acmecharge.example/products/home',
  },
  rounds: [
    {
      id: 'mround_1',
      ordinal: 1,
      state: 'reviewing',
      start_date: '2026-12-01',
      end_date: '2026-12-07',
      effective_start_date: '2026-12-01',
      effective_end_date: '2026-12-07',
    },
  ],
  current_round_id: 'mround_1',
  created_at: '2026-11-22T08:00:00+00:00',
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

test('the start sends the quoted answers, the hash and the kept ads, nothing else', () => {
  const validate = REQUEST_VALIDATORS.start_marketing_test;
  expect(validate(start)).toBe(true);
  expect(validate({ ...start, quote_hash: 'not-a-hash' })).toBe(false);
  expect(
    validate({ ...start, answers: { ...start.answers, duration_days: 6 } })
  ).toBe(false);
  const withoutHash: Record<string, unknown> = { ...start };
  delete withoutHash.quote_hash;
  expect(validate(withoutHash)).toBe(false);
  expect(validate({ ...start, execution_mode: 'byok' })).toBe(false);
});

test('the answer is the new test, as the test read returns it', () => {
  const validate = RESPONSE_VALIDATORS.get('start_marketing_test_201')!;
  expect(validate(created)).toBe(true);
  expect(validate({ ...created, comparison: 'everything' })).toBe(false);
  expect(validate({ ...created, extra: true })).toBe(false);
});

test('the BFF knows the path, forwards If-Match and sends no Location', () => {
  const match = matchAllowlistEntry('POST', [
    'marketing',
    'ideas',
    'mp_ideas',
    'tests',
  ]);
  expect(match?.entry.operationId).toBe('start_marketing_test');
  expect(match?.extractedParams).toEqual({ project_id: 'mp_ideas' });
  const entry = DOMAIN_ALLOWLIST.find(
    (item) => item.operationId === 'start_marketing_test'
  );
  expect(entry).toMatchObject({
    requiresIdempotency: true,
    forwardConditionalHeaders: true,
  });
  // A Location the policy does not declare is a 502 at the BFF.
  expect(entry).not.toHaveProperty('locationShapes');
});

test('the gateway starts the test at the ideas revision the user saw', async () => {
  const fetchMock = jest.fn().mockResolvedValueOnce(
    new Response(JSON.stringify(created), {
      status: 201,
      headers: { 'content-type': 'application/json', etag: '"1"' },
    })
  );
  const started = await makeGateway(fetchMock).startMarketingTest({
    workspaceSlug: 'acme',
    actionInstanceId: 'tests:start:1',
    expectedRevision: 9,
    projectId: 'mp_ideas',
    body: start,
  });
  expect(started).toMatchObject({ kind: 'created' });
  const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(init.method).toBe('POST');
  expect(new URL(path, 'http://localhost').pathname).toBe(
    '/api/domain/v1/w/acme/marketing/ideas/mp_ideas/tests'
  );
  expect(new Headers(init.headers).get('If-Match')).toBe('9');
  expect(JSON.parse(String(init.body))).toEqual(start);
});
