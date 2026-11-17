/**
 * Ads-first verification, ad status and download (#198): the generated validators and the gateway methods.
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

const ad = {
  asset_version_id: 'av_1',
  combination_id: 'c1_1',
  angle_id: 'a1',
  expression_id: 'e1_1',
  status: 'draft',
  reasons: [
    { code: 'facts_unverified', fact_ids: ['f1'], finding_ids: [] },
    { code: 'appearance_unconfirmed', fact_ids: [], finding_ids: [] },
    { code: 'findings_open', fact_ids: [], finding_ids: ['copy_consistent'] },
  ],
  cited_fact_ids: ['f1'],
  appearance_confirmed: false,
  kept: true,
  used_product_photo: false,
  created_at: '2026-11-17T00:00:00+00:00',
  check: null,
};

const project = {
  id: 'mp_1',
  revision: 5,
  state: 'ideas_ready',
  product_id: 'prod_1',
  market: 'United States',
  ad_language: 'en-US',
  understanding,
  ideas: null,
  latest_call: null,
  ads: [ad],
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

test('each ad says its status, why it is not exportable and the facts it stands on', () => {
  const validate = RESPONSE_VALIDATORS.get('get_marketing_ideas_200')!;
  expect(validate(project)).toBe(true);
  expect(
    validate({
      ...project,
      ads: [{ ...ad, status: 'exportable', reasons: [] }],
    })
  ).toBe(true);
  expect(
    validate({
      ...project,
      ads: [
        {
          ...ad,
          status: 'needs_redo',
          reasons: [{ code: 'facts_changed', fact_ids: ['f1'] }],
        },
      ],
    })
  ).toBe(true);
  // An ad from before #198 carries none of the new fields.
  const { reasons, cited_fact_ids, appearance_confirmed, ...older } = ad;
  void reasons;
  void cited_fact_ids;
  void appearance_confirmed;
  expect(validate({ ...project, ads: [older] })).toBe(true);
  expect(
    validate({ ...project, ads: [{ ...ad, reasons: [{ code: 'because' }] }] })
  ).toBe(false);
  expect(validate({ ...project, ads: [{ ...ad, status: 'approved' }] })).toBe(
    false
  );
});

test('editing sends the whole understanding, checking names what the user confirms', () => {
  const edit = REQUEST_VALIDATORS.edit_marketing_ideas_understanding;
  expect(edit({ understanding })).toBe(true);
  expect(edit({ understanding, source_addition: 'From the spec sheet.' })).toBe(
    true
  );
  expect(edit({})).toBe(false);
  const verify = REQUEST_VALIDATORS.verify_marketing_ideas;
  expect(verify({ fact_ids: ['f1'] })).toBe(true);
  expect(
    verify({
      fact_ids: [],
      appearance_confirmed: ['av_1'],
      accepted_findings: [
        {
          asset_version_id: 'av_1',
          check_id: 'maqc_1',
          finding_ids: ['copy_consistent'],
        },
      ],
    })
  ).toBe(true);
  expect(
    verify({
      accepted_findings: [
        { asset_version_id: 'av_1', check_id: 'maqc_1', finding_ids: [] },
      ],
    })
  ).toBe(false);
  expect(verify({ fact_ids: ['f1'], confirmed: true })).toBe(false);
});

test('a download names its ads and returns one signed link for each', () => {
  const request = REQUEST_VALIDATORS.download_marketing_ideas;
  expect(request({ asset_version_ids: ['av_1', 'av_2'] })).toBe(true);
  expect(request({ asset_version_ids: [] })).toBe(false);
  const validate = RESPONSE_VALIDATORS.get('download_marketing_ideas_201')!;
  const download = {
    id: 'mie_1',
    project_id: 'mp_1',
    downloads: [
      {
        asset_version_id: 'av_1',
        combination_id: 'c1_1',
        filename: 'ad-MK-7F3K2Q.png',
        intent_id: 'di_1',
        expires_at: '2026-11-17T00:05:00+00:00',
        download_grant: {
          url: 'https://files.example/signed',
          kind: 'signed_get',
          headers: {},
          content_disposition: 'attachment; filename="ad-MK-7F3K2Q.png"',
        },
      },
    ],
  };
  expect(validate(download)).toBe(true);
  const [first] = download.downloads;
  expect(
    validate({
      ...download,
      downloads: [{ ...first, download_grant: undefined }],
    })
  ).toBe(false);
});

test('the three commands are routed to their own operations', () => {
  const edit = matchAllowlistEntry('PATCH', [
    'marketing',
    'ideas',
    'mp_1',
    'understanding',
  ]);
  expect(edit?.entry.operationId).toBe('edit_marketing_ideas_understanding');
  expect(edit?.extractedParams).toEqual({ project_id: 'mp_1' });
  const verify = matchAllowlistEntry('POST', [
    'marketing',
    'ideas',
    'mp_1',
    'verification',
  ]);
  expect(verify?.entry.operationId).toBe('verify_marketing_ideas');
  const download = matchAllowlistEntry('POST', [
    'marketing',
    'ideas',
    'mp_1',
    'download-intent',
  ]);
  expect(download?.entry.operationId).toBe('download_marketing_ideas');
});

describe('ideas export gateway', () => {
  test('edits the understanding at the revision the user saw', async () => {
    const fetchMock = jest.fn().mockResolvedValueOnce(reply(200, project));
    const edited = await makeGateway(fetchMock).editMarketingIdeasUnderstanding(
      {
        workspaceSlug: 'acme',
        actionInstanceId: 'ideas:edit:1',
        expectedRevision: 5,
        projectId: 'mp_1',
        body: {
          understanding,
        } as Parameters<
          DefaultDomainGateway['editMarketingIdeasUnderstanding']
        >[0]['body'],
      }
    );
    expect(edited).toMatchObject({ kind: 'ok' });
    expect(call(fetchMock)).toEqual({
      method: 'PATCH',
      pathname: '/api/domain/v1/w/acme/marketing/ideas/mp_1/understanding',
      ifMatch: '5',
      body: { understanding },
    });
  });

  test('records a verification at the revision the user saw', async () => {
    const fetchMock = jest.fn().mockResolvedValueOnce(reply(200, project));
    const body = {
      fact_ids: ['f1'],
      appearance_confirmed: ['av_1'],
      accepted_findings: [],
    };
    const verified = await makeGateway(fetchMock).verifyMarketingIdeas({
      workspaceSlug: 'acme',
      actionInstanceId: 'ideas:verify:1',
      expectedRevision: 5,
      projectId: 'mp_1',
      body,
    });
    expect(verified).toMatchObject({ kind: 'ok' });
    expect(call(fetchMock)).toEqual({
      method: 'POST',
      pathname: '/api/domain/v1/w/acme/marketing/ideas/mp_1/verification',
      ifMatch: '5',
      body,
    });
  });

  test('asks for the download of the named ads', async () => {
    const fetchMock = jest.fn().mockResolvedValueOnce(
      reply(201, {
        id: 'mie_1',
        project_id: 'mp_1',
        downloads: [],
      })
    );
    const body = { asset_version_ids: ['av_1'] };
    const downloaded = await makeGateway(fetchMock).downloadMarketingIdeas({
      workspaceSlug: 'acme',
      actionInstanceId: 'ideas:download:1',
      projectId: 'mp_1',
      body,
    });
    expect(downloaded).toMatchObject({ kind: 'created' });
    expect(call(fetchMock)).toEqual({
      method: 'POST',
      pathname: '/api/domain/v1/w/acme/marketing/ideas/mp_1/download-intent',
      ifMatch: null,
      body,
    });
  });
});
