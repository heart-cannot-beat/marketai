/**
 * @jest-environment jsdom
 *
 * Integration test for Draft CAS (Compare-and-Swap) editing. Verifies that
 * `update_generation_draft` sends `If-Match` with the draft's revision, that
 * a 412 response surfaces as a `problem` (not a thrown error), and that
 * `create_generation_preview` honours the same `allowed_actions`-gated,
 * `If-Match`-carrying contract.
 *
 * This exercises the real `useDomainCommand` hook against a mocked
 * `DomainGateway` -- the same seam `__tests__/hooks/domain/useDomainCommand.spec.ts`
 * uses for `create_upload_intent` -- rather than a hand-rolled fetch mock, so
 * the assertions track the actual `callCommandOperation` dispatch
 * (`hooks/domain/useDomainCommand.ts`) instead of a guessed shape.
 */
import { renderHook, act } from '@testing-library/react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';

const mockGateway = {
  updateGenerationDraft: jest.fn(),
  createGenerationPreview: jest.fn(),
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: {
      slug: 'acme',
      workspaceId: 'w1',
      role: 'OWNER',
      membershipId: 'm1',
      displayName: 'Acme',
    },
  }),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));

describe('Draft CAS update', () => {
  beforeEach(() => jest.clearAllMocks());

  it('sends If-Match header with the draft revision', async () => {
    mockGateway.updateGenerationDraft.mockResolvedValue({
      kind: 'success',
      status: 200,
      data: { id: 'draft-1', revision: 2 },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() =>
      useDomainCommand('update_generation_draft')
    );

    await act(async () => {
      await result.current.execute({
        pathParams: { draft_id: 'draft-1' },
        body: { selectors: { product_entry_id: 'p1' } },
        actionInstanceId: 'cas-test-1',
        ifMatch: '"rev-1"',
      });
    });

    expect(mockGateway.updateGenerationDraft).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        draftId: 'draft-1',
        ifMatch: '"rev-1"',
      })
    );
  });

  it('accepts a numeric revision as If-Match (draft.revision is a number)', async () => {
    mockGateway.updateGenerationDraft.mockResolvedValue({
      kind: 'success',
      status: 200,
      data: { id: 'draft-1', revision: 2 },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() =>
      useDomainCommand('update_generation_draft')
    );

    await act(async () => {
      await result.current.execute({
        pathParams: { draft_id: 'draft-1' },
        body: {},
        actionInstanceId: 'cas-test-numeric',
        ifMatch: 1,
      });
    });

    expect(mockGateway.updateGenerationDraft).toHaveBeenCalledWith(
      expect.objectContaining({ ifMatch: 1 })
    );
  });

  it('surfaces 412 Precondition Failed as a problem, not a thrown error', async () => {
    mockGateway.updateGenerationDraft.mockResolvedValue({
      kind: 'problem',
      status: 412,
      problem: {
        type: 'precondition_failed',
        title: 'Precondition Failed',
        status: 412,
        detail: 'Draft was modified by another session',
        instance: '',
        code: 'revision_mismatch',
        trace_id: 't1',
        retryable: false,
        field_errors: [],
      },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() =>
      useDomainCommand('update_generation_draft')
    );

    let outcome: Awaited<ReturnType<typeof result.current.execute>>;
    await act(async () => {
      outcome = await result.current.execute({
        pathParams: { draft_id: 'draft-1' },
        body: {},
        actionInstanceId: 'cas-test-2',
        ifMatch: '"stale-rev"',
      });
    });

    expect(outcome!.kind).toBe('problem');
    expect(result.current.problem?.status).toBe(412);
    expect(result.current.problem?.code).toBe('revision_mismatch');
  });

  it('a stale ifMatch on retry still carries the original (now-stale) revision', async () => {
    // `retry()` replays the last `execute()` params verbatim -- including
    // `ifMatch` -- since it re-sends the same idempotent intent rather than
    // minting a new one. A caller that wants a fresh revision after a 412
    // must re-read the draft and call `execute()` again with the new
    // revision; `retry()` alone cannot resolve a CAS conflict.
    mockGateway.updateGenerationDraft.mockResolvedValue({
      kind: 'problem',
      status: 412,
      problem: {
        type: 'precondition_failed',
        title: 'Precondition Failed',
        status: 412,
        detail: 'Draft was modified',
        instance: '',
        code: 'revision_mismatch',
        trace_id: 't2',
        retryable: false,
        field_errors: [],
      },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() =>
      useDomainCommand('update_generation_draft')
    );

    await act(async () => {
      await result.current.execute({
        pathParams: { draft_id: 'draft-1' },
        body: {},
        actionInstanceId: 'cas-conflict-test',
        ifMatch: '"stale"',
      });
    });
    expect(result.current.problem?.code).toBe('revision_mismatch');

    await act(async () => {
      await result.current.retry();
    });

    const calls = mockGateway.updateGenerationDraft.mock.calls;
    expect(calls).toHaveLength(2);
    expect(calls[1][0]).toEqual(
      expect.objectContaining({ ifMatch: '"stale"' })
    );
  });
});

describe('Preview creation CAS contract', () => {
  beforeEach(() => jest.clearAllMocks());

  it('create_generation_preview sends If-Match with the draft revision', async () => {
    mockGateway.createGenerationPreview.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: { id: 'preview-1', draft_id: 'draft-1', draft_revision: 3 },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() =>
      useDomainCommand('create_generation_preview')
    );

    await act(async () => {
      await result.current.execute({
        pathParams: { draft_id: 'draft-1' },
        actionInstanceId: 'preview-test-1',
        ifMatch: 3,
      });
    });

    expect(mockGateway.createGenerationPreview).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        draftId: 'draft-1',
        ifMatch: 3,
      })
    );
    expect(result.current.result).toEqual({
      id: 'preview-1',
      draft_id: 'draft-1',
      draft_revision: 3,
    });
  });

  it('surfaces a problem when the draft is not eligible for preview (allowed_actions gate failed server-side)', async () => {
    mockGateway.createGenerationPreview.mockResolvedValue({
      kind: 'problem',
      status: 409,
      problem: {
        type: 'draft_not_eligible',
        title: 'Draft not eligible for preview',
        status: 409,
        detail: 'Draft is missing required selectors',
        instance: '',
        code: 'draft_incomplete',
        trace_id: 't3',
        retryable: false,
        field_errors: [],
      },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() =>
      useDomainCommand('create_generation_preview')
    );

    await act(async () => {
      await result.current.execute({
        pathParams: { draft_id: 'draft-1' },
        actionInstanceId: 'preview-test-2',
        ifMatch: 1,
      });
    });

    expect(result.current.problem?.code).toBe('draft_incomplete');
    expect(result.current.result).toBeUndefined();
  });
});
