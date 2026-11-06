import { DefaultDomainGateway } from '../../lib/domain-client/gateway';
import { CsrfTokenManager } from '../../lib/domain-client/csrf';
import { IntentTracker } from '../../lib/domain-client/intent';
import type { components } from '../../lib/domain-client/generated/domain-v1';
import marketingFixture from '../fixtures/marketing-workflow.json';

type CreateUploadIntentRequest =
  components['schemas']['CreateUploadIntentRequest'];

class MemoryStorage implements Storage {
  private store = new Map<string, string>();
  get length() {
    return this.store.size;
  }
  clear() {
    this.store.clear();
  }
  getItem(key: string) {
    return this.store.get(key) ?? null;
  }
  key(index: number) {
    return Array.from(this.store.keys())[index] ?? null;
  }
  removeItem(key: string) {
    this.store.delete(key);
  }
  setItem(key: string, value: string) {
    this.store.set(key, value);
  }
}

function jsonResponse(
  status: number,
  body: unknown,
  headers: Record<string, string> = {}
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });
}

function csrfProblem(): components['schemas']['ProblemDetails'] {
  return {
    type: 'about:blank',
    title: 'CSRF validation failed',
    status: 403,
    code: 'csrf_validation_failed',
    trace_id: 'trace_1',
    retryable: true,
  };
}

function validationProblem(): components['schemas']['ProblemDetails'] {
  return {
    type: 'about:blank',
    title: 'Validation error',
    status: 422,
    code: 'invalid_request',
    trace_id: 'trace_2',
    retryable: false,
  };
}

const uploadBody: CreateUploadIntentRequest = {
  purpose: 'reference_asset',
  owner_kind: 'creative_asset',
  filename: 'photo.png',
  content_type: 'image/png',
  content_length_min: 10,
  content_length_max: 100,
  checksum_sha256_b64: 'abc123==',
};

function makeGateway(fetchMock: jest.Mock) {
  const csrfCalls = { count: 0 };
  const csrf = new CsrfTokenManager({
    ttlMs: 60_000,
    fetchFn: async () => {
      csrfCalls.count++;
      return {
        ok: true,
        status: 200,
        json: async () => ({ token: `csrf_${csrfCalls.count}` }),
      };
    },
  });
  const storage = new MemoryStorage();
  const intents = new IntentTracker({ storage });
  const gateway = new DefaultDomainGateway({
    csrfTokenManager: csrf,
    intentTracker: intents,
    fetchFn: fetchMock as unknown as typeof fetch,
  });
  return { gateway, intents, storage, csrfCalls };
}

test('round and workflow reads forward the explicit image/history/board opt-ins', async () => {
  // A board read with thumbnails: one execution carries its signed 512px link, a placeholder would carry null.
  const bound = JSON.parse(JSON.stringify(marketingFixture.bound));
  const [signed, ...rest] = bound.board.tree.angles.flatMap(
    (a: { expressions: { combinations: { executions: object[] }[] }[] }) =>
      a.expressions.flatMap((x) => x.combinations.flatMap((c) => c.executions))
  ) as Record<string, unknown>[];
  signed.thumbnail = {
    url: 'https://bucket.test/thumb.jpg?sig=1',
    expires_at: '2026-10-24T00:15:00+00:00',
    mime_type: 'image/jpeg',
    width: 512,
    height: 640,
  };
  for (const execution of rest) execution.thumbnail = null;
  const fetchMock = jest.fn().mockImplementation(async (url: string) =>
    jsonResponse(
      200,
      url.includes('/workflow')
        ? {
            project_id: marketingFixture.project.id,
            current: bound,
            rounds: [],
            next_action: 'import_delivery_data',
          }
        : { id: bound.id, data: bound }
    )
  );
  const { gateway } = makeGateway(fetchMock);
  const round = await gateway.getMarketingRound({
    workspaceSlug: 'acme',
    roundId: marketingFixture.bound.id,
    includeImageEdits: true,
    includeVersionHistory: true,
    includeBoard: true,
    includeExecutionPlan: true,
    includeBoardThumbnails: true,
  });
  let url = new URL(fetchMock.mock.calls[0][0], 'https://local.test');
  expect(url.searchParams.get('include_image_edits')).toBe('true');
  expect(url.searchParams.get('include_version_history')).toBe('true');
  expect(url.searchParams.get('include_board')).toBe('true');
  expect(url.searchParams.get('include_execution_plan')).toBe('true');
  expect(url.searchParams.get('include_board_thumbnails')).toBe('true');
  // The strict response validator accepts the server-projected board and its thumbnail links.
  expect(round.kind).toBe('ok');
  const workflow = await gateway.getMarketingWorkflow({
    workspaceSlug: 'acme',
    projectId: marketingFixture.project.id,
    includeImageEdits: true,
    includeVersionHistory: true,
    includeBoard: true,
    includeExecutionPlan: true,
    includeBoardThumbnails: true,
  });
  expect(workflow.kind).toBe('ok');
  url = new URL(fetchMock.mock.calls[1][0], 'https://local.test');
  expect(url.searchParams.get('include_image_edits')).toBe('true');
  expect(url.searchParams.get('include_version_history')).toBe('true');
  expect(url.searchParams.get('include_board')).toBe('true');
  expect(url.searchParams.get('include_execution_plan')).toBe('true');
  expect(url.searchParams.get('include_board_thumbnails')).toBe('true');
  await gateway.getMarketingRound({
    workspaceSlug: 'acme',
    roundId: marketingFixture.bound.id,
  });
  expect(fetchMock.mock.calls[2][0]).not.toContain('?');
});

test('the workflow read asks for the frozen plan its project read compares with (#367)', async () => {
  const fetchMock = jest.fn().mockImplementation(async () =>
    jsonResponse(200, {
      project_id: marketingFixture.project.id,
      current: marketingFixture.bound,
      rounds: [],
      next_action: 'import_delivery_data',
    })
  );
  const { gateway } = makeGateway(fetchMock);
  const workflow = await gateway.getMarketingWorkflow({
    workspaceSlug: 'acme',
    projectId: marketingFixture.project.id,
    includePlanReview: true,
    includeBrandStyle: true,
  });
  expect(workflow.kind).toBe('ok');
  const url = new URL(fetchMock.mock.calls[0][0], 'https://local.test');
  expect(url.searchParams.get('include_plan_review')).toBe('true');
  expect(url.searchParams.get('include_brand_style')).toBe('true');
  // The workflow route has no competitor review parameter; the strict allowlist refuses one.
  expect(url.searchParams.has('include_competitor_review')).toBe(false);
});

describe('DefaultDomainGateway — mutating operations', () => {
  it('does not retain an ambiguous command when CSRF preflight rejects before dispatch', async () => {
    const commandFetch = jest.fn();
    const csrf = new CsrfTokenManager({
      ttlMs: 60_000,
      fetchFn: async () => ({
        ok: false,
        status: 401,
        json: async () => ({ token: '' }),
      }),
    });
    const storage = new MemoryStorage();
    const intents = new IntentTracker({ storage });
    const resolve = jest.spyOn(intents, 'resolve');
    const retain = jest.spyOn(intents, 'retain');
    const gateway = new DefaultDomainGateway({
      csrfTokenManager: csrf,
      intentTracker: intents,
      fetchFn: commandFetch,
    });
    await expect(
      gateway.createUploadIntent({
        workspaceSlug: 'acme',
        actionInstanceId: 'csrf:unsent',
        body: uploadBody,
      })
    ).rejects.toMatchObject({ name: 'CsrfTokenFetchError', status: 401 });
    expect(commandFetch).not.toHaveBeenCalled();
    expect(resolve).toHaveBeenCalledTimes(1);
    expect(retain).not.toHaveBeenCalled();
    expect(storage.getItem('intent:csrf:unsent')).toBeNull();
  });

  it('accepts the contract 201 response when creating a generation draft', async () => {
    const fetchMock = jest.fn().mockResolvedValue(
      jsonResponse(201, {
        id: 'draft-1',
        product_id: 'product-1',
        provider: 'openai',
        model: 'gpt-image-1',
        execution_mode: 'mock',
        state: 'draft',
        revision: 1,
      })
    );
    const { gateway } = makeGateway(fetchMock);

    const result = await gateway.createGenerationDraft({
      workspaceSlug: 'acme',
      actionInstanceId: 'draft:create:1',
      body: {
        product_id: 'product-1',
        provider: 'openai',
        model: 'gpt-image-1',
        execution_mode: 'mock',
      },
    });

    expect(result.kind).toBe('created');
    if (result.kind === 'created') {
      expect(result.status).toBe(201);
    }
  });

  it('accepts the contract 202 response when creating an export package', async () => {
    const fetchMock = jest.fn().mockResolvedValue(
      jsonResponse(202, {
        package_id: 'export-1',
        operation_id: 'operation-1',
        state: 'queued',
      })
    );
    const { gateway } = makeGateway(fetchMock);

    const result = await gateway.createGenerationExportPackage({
      workspaceSlug: 'acme',
      actionInstanceId: 'export:create:1',
      body: { selector: { kind: 'approved_only' } },
    });

    expect(result.kind).toBe('accepted');
    if (result.kind === 'accepted') {
      expect(result.status).toBe(202);
    }
  });

  it('refreshes the CSRF token once on csrf_validation_failed and retries the same request', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse(403, csrfProblem()))
      .mockResolvedValueOnce(
        jsonResponse(201, {
          intent_id: 'ui_1',
          state: 'pending',
          purpose: 'reference_asset',
          expires_at: new Date(Date.now() + 60_000).toISOString(),
          upload_grant: {
            kind: 'signed_put',
            url: 'http://localhost:9000/key',
            headers: {},
            expires_at: new Date().toISOString(),
          },
        })
      );
    const { gateway, csrfCalls } = makeGateway(fetchMock);

    const result = await gateway.createUploadIntent({
      workspaceSlug: 'acme',
      actionInstanceId: 'upload:draft_1',
      body: uploadBody,
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    // Token was refreshed (csrf.clear() forces a second transport fetch).
    expect(csrfCalls.count).toBe(2);
    expect('kind' in result && result.kind).toBe('created');

    // The two fetch calls carried different X-CSRF-Token header values.
    const firstHeaders = fetchMock.mock.calls[0][1].headers as Record<
      string,
      string
    >;
    const secondHeaders = fetchMock.mock.calls[1][1].headers as Record<
      string,
      string
    >;
    expect(firstHeaders['X-CSRF-Token']).toBe('csrf_1');
    expect(secondHeaders['X-CSRF-Token']).toBe('csrf_2');
    // Same X-Intent-Id on both attempts — the retry replays the same
    // command, not a new one.
    expect(firstHeaders['X-Intent-Id']).toBe(secondHeaders['X-Intent-Id']);
  });

  it('surfaces the second failure when the retried request also fails', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse(403, csrfProblem()))
      .mockResolvedValueOnce(jsonResponse(422, validationProblem()));
    const { gateway } = makeGateway(fetchMock);

    const result = await gateway.createUploadIntent({
      workspaceSlug: 'acme',
      actionInstanceId: 'upload:draft_2',
      body: uploadBody,
    });

    // Exactly one retry — no third attempt.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect('kind' in result && result.kind).toBe('problem');
    if ('kind' in result && result.kind === 'problem') {
      expect(result.status).toBe(422);
      expect(result.problem.code).toBe('invalid_request');
    }
  });

  it('resolves the intent on a successful response and narrows its time bounds from expires_at', async () => {
    const expiresAt = new Date(Date.now() + 5 * 60_000).toISOString();
    const fetchMock = jest.fn().mockResolvedValueOnce(
      jsonResponse(201, {
        intent_id: 'ui_2',
        state: 'pending',
        purpose: 'reference_asset',
        expires_at: expiresAt,
        upload_grant: {
          kind: 'signed_put',
          url: 'http://localhost:9000/key',
          headers: {},
          expires_at: new Date().toISOString(),
        },
      })
    );
    const { gateway, intents, storage } = makeGateway(fetchMock);
    const resolveSpy = jest.spyOn(intents, 'resolve');
    const refineSpy = jest.spyOn(intents, 'refineTimeBounds');

    const result = await gateway.createUploadIntent({
      workspaceSlug: 'acme',
      actionInstanceId: 'upload:draft_3',
      body: uploadBody,
    });

    expect('kind' in result && result.kind).toBe('created');
    expect(resolveSpy).toHaveBeenCalledTimes(1);
    expect(resolveSpy.mock.calls[0][0].actionInstanceId).toBe('upload:draft_3');
    expect(refineSpy).toHaveBeenCalledWith('upload:draft_3', {
      safeRetryUntil: Date.parse(expiresAt),
      localRetentionUntil: Date.parse(expiresAt),
    });
    // resolve() removes the persisted intent.
    expect(storage.getItem('intent:upload:draft_3')).toBeNull();
  });

  it('resolves the intent on a definitive (non-CSRF) problem response', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse(422, validationProblem()));
    const { gateway, intents, storage } = makeGateway(fetchMock);
    const resolveSpy = jest.spyOn(intents, 'resolve');

    const result = await gateway.createUploadIntent({
      workspaceSlug: 'acme',
      actionInstanceId: 'upload:draft_4',
      body: uploadBody,
    });

    expect('kind' in result && result.kind).toBe('problem');
    expect(resolveSpy).toHaveBeenCalledTimes(1);
    expect(storage.getItem('intent:upload:draft_4')).toBeNull();
  });

  it('retains (does not resolve) the intent when fetch throws a network error', async () => {
    const networkError = new Error('network down');
    const fetchMock = jest.fn().mockRejectedValueOnce(networkError);
    const { gateway, intents, storage } = makeGateway(fetchMock);
    const retainSpy = jest.spyOn(intents, 'retain');
    const resolveSpy = jest.spyOn(intents, 'resolve');

    await expect(
      gateway.createUploadIntent({
        workspaceSlug: 'acme',
        actionInstanceId: 'upload:draft_5',
        body: uploadBody,
      })
    ).rejects.toThrow('network down');

    expect(retainSpy).toHaveBeenCalledTimes(1);
    expect(retainSpy.mock.calls[0][0].actionInstanceId).toBe('upload:draft_5');
    expect(resolveSpy).not.toHaveBeenCalled();

    const raw = storage.getItem('intent:upload:draft_5');
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw!).status).toBe('retained');
  });

  it('coalesces concurrent calls for the same actionInstanceId into a single request', async () => {
    let resolveFetch: (response: Response) => void;
    const fetchMock = jest.fn().mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      })
    );
    const { gateway, intents } = makeGateway(fetchMock);
    const acquireSpy = jest.spyOn(intents, 'acquire');

    const first = gateway.createUploadIntent({
      workspaceSlug: 'acme',
      actionInstanceId: 'upload:draft_6',
      body: uploadBody,
    });
    const second = gateway.createUploadIntent({
      workspaceSlug: 'acme',
      actionInstanceId: 'upload:draft_6',
      body: uploadBody,
    });

    resolveFetch!(
      jsonResponse(201, {
        intent_id: 'ui_6',
        state: 'pending',
        purpose: 'reference_asset',
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        upload_grant: {
          kind: 'signed_put',
          url: 'http://localhost:9000/key',
          headers: {},
          expires_at: new Date().toISOString(),
        },
      })
    );

    const [firstResult, secondResult] = await Promise.all([first, second]);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(acquireSpy).toHaveBeenCalledTimes(1);
    expect(firstResult).toBe(secondResult);
  });

  it('forwards Draft revisions as If-Match on conditional Library commands', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, { id: 'entry-1' }));
    const { gateway } = makeGateway(fetchMock);

    await gateway.saveLibraryDraft({
      workspaceSlug: 'acme',
      actionInstanceId: 'save:entry-1',
      entryId: 'entry-1',
      ifMatch: 7,
      body: { content: { display_name: 'Updated' } },
    });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe('PUT');
    expect((init.headers as Record<string, string>)['If-Match']).toBe('7');
  });

  it('sends no step-up header for a former step-up Library command (ADR 0015)', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse(201, { id: 'version-1' }));
    const { gateway } = makeGateway(fetchMock);

    await gateway.publishLibraryEntry({
      workspaceSlug: 'acme',
      actionInstanceId: 'publish:entry-1',
      entryId: 'entry-1',
      ifMatch: 4,
      body: {
        impact_id: 'impact-1',
        impact_hash: 'hash-1',
        acknowledged_warnings: [],
      },
    });

    const headers = fetchMock.mock.calls[0][1].headers as Record<
      string,
      string
    >;
    expect(headers['If-Match']).toBe('4');
    expect(headers['X-Step-Up-Grant']).toBeUndefined();
    expect(headers['X-Step-Up-Assertion']).toBeUndefined();
  });
});

describe('DefaultDomainGateway — conditional headers', () => {
  describe('If-Match on mutating commands', () => {
    it('sends If-Match header when ifMatch is provided to updateGenerationDraft', async () => {
      const fetchMock = jest
        .fn()
        .mockResolvedValueOnce(
          jsonResponse(200, { id: 'draft_1', state: 'open' })
        );
      const { gateway } = makeGateway(fetchMock);

      const result = await gateway.updateGenerationDraft({
        workspaceSlug: 'acme',
        actionInstanceId: 'update-draft:1',
        draftId: 'draft_1',
        body: { product_id: 'prod_1' } as any,
        ifMatch: '"rev_42"',
      });

      expect(result.kind).toBe('ok');
      const headers = fetchMock.mock.calls[0][1].headers as Record<
        string,
        string
      >;
      expect(headers['If-Match']).toBe('"rev_42"');
    });

    it('does not send If-Match header when ifMatch is omitted', async () => {
      const fetchMock = jest
        .fn()
        .mockResolvedValueOnce(
          jsonResponse(200, { id: 'draft_1', state: 'open' })
        );
      const { gateway } = makeGateway(fetchMock);

      await gateway.updateGenerationDraft({
        workspaceSlug: 'acme',
        actionInstanceId: 'update-draft:2',
        draftId: 'draft_1',
        body: { product_id: 'prod_1' } as any,
      });

      const headers = fetchMock.mock.calls[0][1].headers as Record<
        string,
        string
      >;
      expect(headers['If-Match']).toBeUndefined();
    });

    it('sends If-Match header on a recovery command (retryGenerationRunFailedItems)', async () => {
      const fetchMock = jest
        .fn()
        .mockResolvedValueOnce(
          jsonResponse(200, { run_id: 'run_1', accepted_count: 3 })
        );
      const { gateway } = makeGateway(fetchMock);

      const result = await gateway.retryGenerationRunFailedItems({
        workspaceSlug: 'acme',
        actionInstanceId: 'retry:run_1',
        runId: 'run_1',
        body: { item_ids: ['i1', 'i2'] } as any,
        ifMatch: '"run_rev_7"',
      });

      expect(result.kind).toBe('ok');
      const headers = fetchMock.mock.calls[0][1].headers as Record<
        string,
        string
      >;
      expect(headers['If-Match']).toBe('"run_rev_7"');
    });

    it('surfaces 412 Precondition Failed as a typed problem result', async () => {
      const problem412 = {
        type: 'about:blank',
        title: 'Precondition Failed',
        status: 412,
        code: 'precondition_failed',
        detail: 'ETag mismatch',
        instance: '/drafts/draft_1',
        trace_id: 'trace_412',
        retryable: false,
        field_errors: [],
      };
      const fetchMock = jest
        .fn()
        .mockResolvedValueOnce(jsonResponse(412, problem412));
      const { gateway } = makeGateway(fetchMock);

      const result = await gateway.updateGenerationDraft({
        workspaceSlug: 'acme',
        actionInstanceId: 'update-draft:3',
        draftId: 'draft_1',
        body: { product_id: 'prod_1' } as any,
        ifMatch: '"stale_rev"',
      });

      expect(result.kind).toBe('problem');
      if (result.kind === 'problem') {
        expect(result.status).toBe(412);
        expect(result.problem.code).toBe('precondition_failed');
      }
    });
  });

  describe('If-None-Match on safe reads', () => {
    it('sends If-None-Match header and returns not_modified on 304', async () => {
      const fetchMock = jest.fn().mockResolvedValueOnce(
        new Response(null, {
          status: 304,
          headers: { etag: '"rev_5"' },
        })
      );
      const { gateway } = makeGateway(fetchMock);

      const result = await gateway.getGenerationDraft({
        workspaceSlug: 'acme',
        draftId: 'draft_1',
        ifNoneMatch: '"rev_5"',
      });

      expect(result.kind).toBe('not_modified');
      expect(result.status).toBe(304);
      const headers = fetchMock.mock.calls[0][1].headers as Record<
        string,
        string
      >;
      expect(headers['If-None-Match']).toBe('"rev_5"');
    });

    it('does not send If-None-Match when omitted', async () => {
      const fetchMock = jest
        .fn()
        .mockResolvedValueOnce(
          jsonResponse(200, { id: 'draft_1', state: 'open' })
        );
      const { gateway } = makeGateway(fetchMock);

      await gateway.getGenerationDraft({
        workspaceSlug: 'acme',
        draftId: 'draft_1',
      });

      const headers = fetchMock.mock.calls[0][1].headers as Record<
        string,
        string
      >;
      expect(headers['If-None-Match']).toBeUndefined();
    });

    it('returns ok with data when server responds 200 despite If-None-Match', async () => {
      const draftBody = { id: 'draft_1', state: 'open', revision: 6 };
      const fetchMock = jest
        .fn()
        .mockResolvedValueOnce(
          jsonResponse(200, draftBody, { etag: '"rev_6"' })
        );
      const { gateway } = makeGateway(fetchMock);

      const result = await gateway.getGenerationDraft({
        workspaceSlug: 'acme',
        draftId: 'draft_1',
        ifNoneMatch: '"rev_5"',
      });

      expect(result.kind).toBe('ok');
      if (result.kind === 'ok') {
        expect(result.data).toEqual(draftBody);
        expect(result.meta.etag).toBe('"rev_6"');
      }
    });

    it('sends If-None-Match on getGenerationRun (another conditional read)', async () => {
      const fetchMock = jest.fn().mockResolvedValueOnce(
        new Response(null, {
          status: 304,
          headers: { etag: '"run_rev_3"' },
        })
      );
      const { gateway } = makeGateway(fetchMock);

      const result = await gateway.getGenerationRun({
        workspaceSlug: 'acme',
        runId: 'run_1',
        ifNoneMatch: '"run_rev_3"',
      });

      expect(result.kind).toBe('not_modified');
      const headers = fetchMock.mock.calls[0][1].headers as Record<
        string,
        string
      >;
      expect(headers['If-None-Match']).toBe('"run_rev_3"');
    });
  });
});

describe('DefaultDomainGateway — Owner credentials', () => {
  it('reads the credential list as a plain GET', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(200, [
          { credential_version_id: 'cv_1', credential_key: 'openai_api_key' },
        ])
      );
    const { gateway } = makeGateway(fetchMock);

    const result = await gateway.listCredentials({ workspaceSlug: 'acme' });

    expect(result.kind).toBe('ok');
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe('/api/domain/v1/w/acme/credentials');
    expect(init.method).toBe('GET');
    const headers = init.headers as Record<string, string>;
    expect(headers['X-Intent-Id']).toBeUndefined();
    expect(headers['X-Step-Up-Grant']).toBeUndefined();
  });

  it('creates a credential with the contract 200 and no step-up header', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(200, { credential_version_id: 'cv_1', version_no: 1 })
      );
    const { gateway } = makeGateway(fetchMock);

    const result = await gateway.createCredential({
      workspaceSlug: 'acme',
      actionInstanceId: 'credential:create:1',
      body: {
        credential_key: 'openai_api_key',
        provider: 'openai',
        plaintext_b64: 'c2stcHJvai1hYmM=',
        display_label: null,
      },
    });

    expect(result.kind).toBe('ok');
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe('/api/domain/v1/w/acme/credentials');
    expect(init.method).toBe('POST');
    const headers = init.headers as Record<string, string>;
    expect(headers['X-Step-Up-Grant']).toBeUndefined();
    expect(headers['X-Step-Up-Assertion']).toBeUndefined();
    expect(headers['X-Intent-Id']).toBeTruthy();
    expect(JSON.parse(String(init.body))).toEqual({
      credential_key: 'openai_api_key',
      provider: 'openai',
      plaintext_b64: 'c2stcHJvai1hYmM=',
      display_label: null,
    });
  });

  it('binds, rotates and revokes through their own paths', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, { binding_id: 'cb_1' }))
      .mockResolvedValueOnce(
        jsonResponse(200, { credential_version_id: 'cv_2', version_no: 2 })
      )
      .mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    const { gateway } = makeGateway(fetchMock);

    const bound = await gateway.bindCredential({
      workspaceSlug: 'acme',
      actionInstanceId: 'credential:bind:1',
      credentialVersionId: 'cv_1',
      scope: 'generation',
      body: { provider: 'openai' },
    });
    const rotated = await gateway.rotateCredential({
      workspaceSlug: 'acme',
      actionInstanceId: 'credential:rotate:1',
      credentialKey: 'openai_api_key',
      body: { new_plaintext_b64: 'bmV3' },
    });
    const revoked = await gateway.revokeCredential({
      workspaceSlug: 'acme',
      actionInstanceId: 'credential:revoke:1',
      credentialVersionId: 'cv_2',
      body: { reason: 'removed from workspace settings' },
    });

    expect([bound.kind, rotated.kind, revoked.kind]).toEqual([
      'ok',
      'ok',
      'ok',
    ]);
    const calls = fetchMock.mock.calls as Array<[string, RequestInit]>;
    expect(calls.map(([path, init]) => [init.method, path])).toEqual([
      ['PUT', '/api/domain/v1/w/acme/credentials/cv_1/bindings/generation'],
      ['POST', '/api/domain/v1/w/acme/credentials/openai_api_key/rotate'],
      ['POST', '/api/domain/v1/w/acme/credentials/cv_2/revoke'],
    ]);
    expect(
      calls.map(
        ([, init]) =>
          (init.headers as Record<string, string>)['X-Step-Up-Grant']
      )
    ).toEqual([undefined, undefined, undefined]);
  });
});
