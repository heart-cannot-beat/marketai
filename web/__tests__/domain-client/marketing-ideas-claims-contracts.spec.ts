/**
 * Changing and deleting a claim, the confirmed audience and claims without a
 * source (#224): the generated validators and the gateway methods.
 */
import {
  RESPONSE_VALIDATORS,
  REQUEST_VALIDATORS,
} from '@/lib/domain-client/generated/validators';
import { matchAllowlistEntry } from '@/lib/domain-client/request-pipeline';
import { DefaultDomainGateway } from '@/lib/domain-client/gateway';
import { CsrfTokenManager } from '@/lib/domain-client/csrf';
import { IntentTracker } from '@/lib/domain-client/intent';

const understanding = {
  product_name: 'Acme Charge Home',
  category_need: 'Charge an electric car at home.',
  product_choice_reason: 'Scheduling from the app.',
  audience: 'Drivers with a driveway',
  situations: ['Overnight charging'],
  objections: [],
  facts: [
    {
      id: 'f1',
      statement: 'Charging can be scheduled from the Acme Charge app.',
      source_quote: 'Schedule charging from the Acme Charge app.',
      confirmation: 'confirmed',
    },
  ],
  inferences: [
    {
      id: 'i1',
      statement: 'Drivers charge overnight.',
      rationale: 'A guess.',
      fact_ids: ['f1'],
    },
  ],
  unknowns: [],
};

const project = {
  id: 'mp_1',
  revision: 5,
  state: 'ideas_ready',
  product_id: 'prod_1',
  market: 'United States',
  ad_language: 'en-US',
  understanding: {
    ...understanding,
    unsupported_claims: [
      { id: 'u1', text: 'Charges 3x faster than a wall socket' },
    ],
  },
  audience_confirmed: true,
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

function call(fetchMock: jest.Mock) {
  const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit?];
  const headers = new Headers(init?.headers);
  return {
    method: init?.method ?? 'GET',
    pathname: new URL(path, 'http://localhost').pathname,
    ifMatch: headers.get('If-Match'),
    body: init?.body ? JSON.parse(String(init.body)) : undefined,
  };
}

test('the ideas say whether the audience is confirmed and what was read without a source', () => {
  const validate = RESPONSE_VALIDATORS.get('get_marketing_ideas_200')!;
  expect(validate(project)).toBe(true);
  // Ideas from before #224 carry neither.
  const { audience_confirmed, ...older } = project;
  void audience_confirmed;
  expect(validate({ ...older, understanding })).toBe(true);
  expect(
    validate({
      ...project,
      understanding: {
        ...project.understanding,
        unsupported_claims: [{ id: 'u1' }],
      },
    })
  ).toBe(false);
});

test('a claim is changed as a whole, in the user words', () => {
  const edit = REQUEST_VALIDATORS.edit_marketing_ideas_claim;
  expect(
    edit({
      proposition: 'Wake up to a charged car',
      rationale: 'Most owners charge overnight.',
      fact_ids: ['f1'],
    })
  ).toBe(true);
  expect(edit({ proposition: 'Only words', rationale: 'Why.' })).toBe(true);
  expect(edit({ proposition: '', rationale: 'Why.', fact_ids: [] })).toBe(
    false
  );
  expect(edit({ rationale: 'Why.', fact_ids: [] })).toBe(false);
  expect(
    edit({ proposition: 'A', rationale: 'B', fact_ids: [], headline: 'C' })
  ).toBe(false);
  const verify = REQUEST_VALIDATORS.verify_marketing_ideas;
  expect(verify({ audience_confirmed: true })).toBe(true);
});

test('the two claim commands share a path and are told apart by method', () => {
  const edit = matchAllowlistEntry('PATCH', [
    'marketing',
    'ideas',
    'mp_1',
    'claims',
    'a1',
  ]);
  expect(edit?.entry.operationId).toBe('edit_marketing_ideas_claim');
  expect(edit?.extractedParams).toEqual({ project_id: 'mp_1', angle_id: 'a1' });
  const remove = matchAllowlistEntry('DELETE', [
    'marketing',
    'ideas',
    'mp_1',
    'claims',
    'a1',
  ]);
  expect(remove?.entry.operationId).toBe('delete_marketing_ideas_claim');
});

describe('claim gateway', () => {
  test('changes a claim at the revision the user saw', async () => {
    const fetchMock = jest.fn().mockResolvedValueOnce(reply(200, project));
    const body = {
      proposition: 'Wake up to a charged car',
      rationale: 'Most owners charge overnight.',
      fact_ids: ['f1'],
    };
    const edited = await makeGateway(fetchMock).editMarketingIdeasClaim({
      workspaceSlug: 'acme',
      actionInstanceId: 'ideas:claim:1',
      expectedRevision: 5,
      projectId: 'mp_1',
      angleId: 'a1',
      body,
    });
    expect(edited).toMatchObject({ kind: 'ok' });
    expect(call(fetchMock)).toEqual({
      method: 'PATCH',
      pathname: '/api/domain/v1/w/acme/marketing/ideas/mp_1/claims/a1',
      ifMatch: '5',
      body,
    });
  });

  test('deletes a claim at the revision the user saw, with no body', async () => {
    const fetchMock = jest.fn().mockResolvedValueOnce(reply(200, project));
    const deleted = await makeGateway(fetchMock).deleteMarketingIdeasClaim({
      workspaceSlug: 'acme',
      actionInstanceId: 'ideas:claim-delete:1',
      expectedRevision: 5,
      projectId: 'mp_1',
      angleId: 'a1',
    });
    expect(deleted).toMatchObject({ kind: 'ok' });
    expect(call(fetchMock)).toEqual({
      method: 'DELETE',
      pathname: '/api/domain/v1/w/acme/marketing/ideas/mp_1/claims/a1',
      ifMatch: '5',
      body: undefined,
    });
  });
});
