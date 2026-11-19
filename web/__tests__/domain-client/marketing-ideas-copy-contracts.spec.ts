/**
 * Changing one ad's headline and body copy (#228): the generated request
 * validator, the BFF allowlist entry and the gateway method.
 */
import { REQUEST_VALIDATORS } from '@/lib/domain-client/generated/validators';
import { DOMAIN_ALLOWLIST } from '@/lib/domain-client/generated/allowlist';
import { matchAllowlistEntry } from '@/lib/domain-client/request-pipeline';
import { DefaultDomainGateway } from '@/lib/domain-client/gateway';
import { CsrfTokenManager } from '@/lib/domain-client/csrf';
import { IntentTracker } from '@/lib/domain-client/intent';

const project = {
  id: 'mp_1',
  revision: 6,
  state: 'ideas_ready',
  product_id: 'prod_1',
  market: 'United States',
  ad_language: 'en-US',
  understanding: null,
  audience_confirmed: false,
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
    headers: { 'content-type': 'application/json', etag: '"6"' },
  });
}

test('the copy is changed as a whole: a headline and a body, nothing else', () => {
  const edit = REQUEST_VALIDATORS.edit_marketing_ideas_copy;
  expect(
    edit({
      headline: 'Charge on your schedule',
      body_copy: 'Set overnight charging from the Acme Charge app.',
    })
  ).toBe(true);
  expect(edit({ headline: '', body_copy: 'Body.' })).toBe(false);
  expect(edit({ headline: 'Only a headline' })).toBe(false);
  expect(edit({ headline: 'x'.repeat(2001), body_copy: 'Body.' })).toBe(false);
  // The claim, the way of telling and the image brief are not part of it.
  expect(
    edit({ headline: 'A', body_copy: 'B', proposition: 'Another claim' })
  ).toBe(false);
});

test('the BFF knows the path, forwards If-Match and sends no Location', () => {
  const match = matchAllowlistEntry('PATCH', [
    'marketing',
    'ideas',
    'mp_1',
    'combinations',
    'c1_1',
  ]);
  expect(match?.entry.operationId).toBe('edit_marketing_ideas_copy');
  expect(match?.extractedParams).toEqual({
    project_id: 'mp_1',
    combination_id: 'c1_1',
  });
  const entry = DOMAIN_ALLOWLIST.find(
    (item) => item.operationId === 'edit_marketing_ideas_copy'
  );
  expect(entry).toMatchObject({
    requiresIdempotency: true,
    forwardConditionalHeaders: true,
  });
  // A Location the policy does not declare is a 502 at the BFF.
  expect(entry).not.toHaveProperty('locationShapes');
});

test('the gateway changes the copy at the revision the user saw', async () => {
  const fetchMock = jest.fn().mockResolvedValueOnce(reply(200, project));
  const body = {
    headline: 'Charge on your schedule',
    body_copy: 'Set overnight charging from the Acme Charge app.',
  };
  const edited = await makeGateway(fetchMock).editMarketingIdeasCopy({
    workspaceSlug: 'acme',
    actionInstanceId: 'ideas:copy:1',
    expectedRevision: 5,
    projectId: 'mp_1',
    combinationId: 'c1_1',
    body,
  });
  expect(edited).toMatchObject({ kind: 'ok' });
  const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(init.method).toBe('PATCH');
  expect(new URL(path, 'http://localhost').pathname).toBe(
    '/api/domain/v1/w/acme/marketing/ideas/mp_1/combinations/c1_1'
  );
  expect(new Headers(init.headers).get('If-Match')).toBe('5');
  expect(JSON.parse(String(init.body))).toEqual(body);
});
