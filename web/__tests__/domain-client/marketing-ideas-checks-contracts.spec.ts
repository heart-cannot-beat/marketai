/**
 * Ads-first ideas checks (#197): the generated validators and the gateway methods.
 */
import {
  RESPONSE_VALIDATORS,
  REQUEST_VALIDATORS,
} from '@/lib/domain-client/generated/validators';
import { matchAllowlistEntry } from '@/lib/domain-client/request-pipeline';
import { DefaultDomainGateway } from '@/lib/domain-client/gateway';
import { CsrfTokenManager } from '@/lib/domain-client/csrf';
import { IntentTracker } from '@/lib/domain-client/intent';

const check = {
  id: 'maqc_1',
  state: 'succeeded',
  execution_mode: 'byok',
  source: 'ai_visual_suggestion',
  suggestion: 'needs_changes',
  findings: [
    {
      check_id: 'copy_consistent',
      description: 'Image and accompanying copy agree',
      status: 'fail',
      confidence: 'high',
      gate_reason: 'rendered_text_mismatch',
      visual_evidence: 'The headline reads "Charge overnigth".',
      rationale: 'The rendered headline differs from the ad copy.',
      suggested_fix:
        'Render the confirmed copy exactly; correct: “overnigth” → “overnight”',
      fact_ids: [],
      unsupported_claims: [],
      rendered_text_diff: [
        {
          field: 'headline',
          expected: 'Charge overnight',
          rendered: 'Charge overnigth',
          changes: [{ expected: 'overnight', rendered: 'overnigth' }],
        },
      ],
    },
  ],
  understanding_hash: 'h1',
  fact_ids: ['f1'],
  language: 'en-US',
  maximum_cost: '0.10',
  error_code: null,
  created_at: '2026-11-17T00:00:00+00:00',
};

const project = {
  id: 'mp_1',
  revision: 4,
  state: 'ideas_ready',
  product_id: 'prod_1',
  market: 'United States',
  ad_language: 'en-US',
  understanding: null,
  ideas: null,
  latest_call: null,
  ads: [
    {
      asset_version_id: 'av_1',
      combination_id: 'c1_1',
      angle_id: 'a1',
      expression_id: 'e1_1',
      status: 'draft',
      kept: false,
      used_product_photo: false,
      created_at: '2026-11-17T00:00:00+00:00',
      check,
    },
  ],
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

test('each ad carries its newest check, with only the findings that need attention', () => {
  const validate = RESPONSE_VALIDATORS.get('get_marketing_ideas_200')!;
  expect(validate(project)).toBe(true);
  const [ad] = project.ads;
  expect(validate({ ...project, ads: [{ ...ad, check: null }] })).toBe(true);
  const without = Object.fromEntries(
    Object.entries(ad).filter(([key]) => key !== 'check')
  );
  expect(validate({ ...project, ads: [without] })).toBe(true);
  const passed = { ...check.findings[0], status: 'pass' };
  expect(
    validate({
      ...project,
      ads: [{ ...ad, check: { ...check, findings: [passed] } }],
    })
  ).toBe(false);
  expect(
    validate({
      ...project,
      ads: [{ ...ad, check: { ...check, state: 'approved' } }],
    })
  ).toBe(false);
});

test('the quote names the maximum and the ads it counts', () => {
  const validate = RESPONSE_VALIDATORS.get(
    'get_marketing_ideas_checks_quote_200'
  )!;
  const quote = {
    available: false,
    unavailable_reason: 'credential_required',
    execution_mode: 'byok',
    provider: 'openai',
    model: 'vision-test',
    quantity: 0,
    maximum_cost: '0',
    currency: 'USD',
    quote_hash: 'a'.repeat(64),
    asset_version_ids: [],
    skipped: [{ asset_version_id: 'av_2', code: 'product_reference_changed' }],
  };
  expect(validate(quote)).toBe(true);
  expect(validate({ ...quote, execution_mode: 'free' })).toBe(false);
});

test('starting needs the mode and the confirmed quote', () => {
  const start = REQUEST_VALIDATORS.start_marketing_ideas_checks;
  expect(start({ execution_mode: 'byok', quote_hash: 'a'.repeat(64) })).toBe(
    true
  );
  expect(start({ execution_mode: 'byok' })).toBe(false);
  expect(start({ execution_mode: 'byok', quote_hash: 'short' })).toBe(false);
});

test('the quote is not read as the ideas themselves', () => {
  const quote = matchAllowlistEntry('GET', [
    'marketing',
    'ideas',
    'mp_1',
    'checks',
    'quote',
  ]);
  expect(quote?.entry.operationId).toBe('get_marketing_ideas_checks_quote');
  expect(quote?.extractedParams).toEqual({ project_id: 'mp_1' });
  const start = matchAllowlistEntry('POST', [
    'marketing',
    'ideas',
    'mp_1',
    'checks',
  ]);
  expect(start?.entry.operationId).toBe('start_marketing_ideas_checks');
});

describe('ideas checks gateway', () => {
  test('reads the quote for a mode', async () => {
    const fetchMock = jest.fn().mockResolvedValue(
      reply(200, {
        available: true,
        unavailable_reason: null,
        execution_mode: 'mock',
        provider: 'mock',
        model: 'marketing-qc-example-v1',
        quantity: 6,
        maximum_cost: '0',
        currency: 'USD',
        quote_hash: 'a'.repeat(64),
        asset_version_ids: ['av_1'],
        skipped: [],
      })
    );
    const result = await makeGateway(fetchMock).getMarketingIdeasChecksQuote({
      workspaceSlug: 'acme',
      projectId: 'mp_1',
      executionMode: 'mock',
    });
    expect(result).toMatchObject({ kind: 'ok' });
    expect(call(fetchMock)).toEqual({
      method: 'GET',
      pathname: '/api/domain/v1/w/acme/marketing/ideas/mp_1/checks/quote',
      query: { execution_mode: 'mock' },
      body: undefined,
    });
  });

  test('starts the checks', async () => {
    const fetchMock = jest.fn().mockResolvedValueOnce(reply(202, project));
    const body = {
      execution_mode: 'byok' as const,
      quote_hash: 'a'.repeat(64),
    };
    const started = await makeGateway(fetchMock).startMarketingIdeasChecks({
      workspaceSlug: 'acme',
      actionInstanceId: 'ideas-checks:start:1',
      projectId: 'mp_1',
      body,
    });
    expect(started).toMatchObject({ kind: 'accepted' });
    expect(call(fetchMock)).toEqual({
      method: 'POST',
      pathname: '/api/domain/v1/w/acme/marketing/ideas/mp_1/checks',
      query: {},
      body,
    });
  });
});

test('a first batch checks its images before it is done (#197)', () => {
  const validate = RESPONSE_VALIDATORS.get('get_marketing_first_batch_200')!;
  const batch = {
    id: 'mfb_1',
    operation_id: 'op_1',
    state: 'checking',
    needs_attention: false,
    attention: null,
    resume: null,
    intake_id: 'pin_1',
    product_id: 'prod_1',
    project_id: 'mp_1',
    market: 'United States',
    ad_language: 'en-US',
    strategy_language: 'en-US',
    execution_mode: 'byok',
    selected_variant: null,
    confirmed_max_cost: '0.95',
    reserved_total: '0.85',
    currency: 'USD',
    image_provider: 'openai',
    image_model: 'gpt-image-1.5',
    ads_planned: 6,
    created_at: '2026-11-17T00:00:00+00:00',
    updated_at: '2026-11-17T00:00:00+00:00',
  };
  expect(validate(batch)).toBe(true);
  expect(
    validate({
      ...batch,
      state: 'needs_attention',
      needs_attention: true,
      attention: {
        reason: 'step_failed',
        step: 'checks',
        code: 'qc_output_invalid',
        variants: [],
        combination_ids: ['c1_1'],
        resumable: true,
      },
    })
  ).toBe(true);
  const quote = RESPONSE_VALIDATORS.get('get_marketing_first_batch_quote_200')!;
  expect(
    quote({
      available: true,
      unavailable_reason: null,
      execution_mode: 'byok',
      maximum_cost: '0.95',
      currency: 'USD',
      quote_hash: 'q1',
      image_provider: 'openai',
      image_model: 'gpt-image-1.5',
      steps: [
        {
          step: 'checks',
          quantity: 6,
          unit_maximum: '0.05',
          maximum_cost: '0.3',
        },
      ],
    })
  ).toBe(true);
});
