/**
 * @jest-environment jsdom
 */
import { renderHook, act } from '@testing-library/react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';

const mockGateway = {
  createUploadIntent: jest.fn(),
  createLibraryInternalEntry: jest.fn(),
  submitLocalizationClaimDecisions: jest.fn(),
  approveLocalizationCopy: jest.fn(),
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

describe('useDomainCommand', () => {
  beforeEach(() => jest.clearAllMocks());

  it('executes a command and returns result', async () => {
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'success',
      status: 201,
      data: { upload_intent_id: 'ui1' },
      meta: {},
    });

    const { result } = renderHook(() =>
      useDomainCommand('create_upload_intent')
    );

    await act(async () => {
      await result.current.execute({
        body: { owner_kind: 'product', owner_id: 'p1' } as any,
        actionInstanceId: 'test-action-1',
      });
    });

    expect(result.current.result).toEqual({ upload_intent_id: 'ui1' });
    expect(result.current.problem).toBeUndefined();
    expect(mockGateway.createUploadIntent).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      actionInstanceId: 'test-action-1',
      body: { owner_kind: 'product', owner_id: 'p1' },
    });
  });

  it('routes internal library creation with its root context', async () => {
    mockGateway.createLibraryInternalEntry.mockResolvedValue({
      kind: 'success',
      status: 201,
      data: { id: 'offer-1' },
      meta: {},
    });

    const { result } = renderHook(() =>
      useDomainCommand('create_library_internal_entry')
    );

    await act(async () => {
      await result.current.execute({
        pathParams: { root_entry_id: 'product-1' },
        body: {
          entry_type: 'offer',
          display_name: 'Offer',
          parent_type: 'product',
          parent_id: 'product-1',
        },
        actionInstanceId: 'create-internal-1',
      });
    });

    expect(mockGateway.createLibraryInternalEntry).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      actionInstanceId: 'create-internal-1',
      rootEntryId: 'product-1',
      body: {
        entry_type: 'offer',
        display_name: 'Offer',
        parent_type: 'product',
        parent_id: 'product-1',
      },
    });
  });

  it('surfaces problem on error response', async () => {
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'problem',
      status: 409,
      problem: { type: 'conflict', title: 'Conflict', status: 409 },
      meta: {},
    });

    const { result } = renderHook(() =>
      useDomainCommand('create_upload_intent')
    );

    await act(async () => {
      await result.current.execute({
        body: {} as any,
        actionInstanceId: 'test-action-2',
      });
    });

    expect(result.current.result).toBeUndefined();
    expect(result.current.problem?.status).toBe(409);
  });

  it('surfaces reconciliation when returned', async () => {
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'reconciliation_required',
      actionInstanceId: 'test-action-3',
      staleSince: 1234,
    });

    const { result } = renderHook(() =>
      useDomainCommand('create_upload_intent')
    );

    await act(async () => {
      await result.current.execute({
        body: {} as any,
        actionInstanceId: 'test-action-3',
      });
    });

    expect(result.current.reconciliation).toBeDefined();
    expect(result.current.reconciliation?.staleSince).toBe(1234);
  });

  it('retry reuses the same actionInstanceId', async () => {
    mockGateway.createUploadIntent
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce({
        kind: 'success',
        status: 201,
        data: {},
        meta: {},
      });

    const { result } = renderHook(() =>
      useDomainCommand('create_upload_intent')
    );

    await act(async () => {
      await result.current
        .execute({
          body: {} as any,
          actionInstanceId: 'retry-test',
        })
        .catch(() => {});
    });

    await act(async () => {
      await result.current.retry();
    });

    const calls = mockGateway.createUploadIntent.mock.calls;
    expect(calls[0][0].actionInstanceId).toBe('retry-test');
    expect(calls[1][0].actionInstanceId).toBe('retry-test');
  });

  it('reset clears result and problem', async () => {
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'success',
      status: 201,
      data: { id: '1' },
      meta: {},
    });

    const { result } = renderHook(() =>
      useDomainCommand('create_upload_intent')
    );

    await act(async () => {
      await result.current.execute({
        body: {} as any,
        actionInstanceId: 'reset-test',
      });
    });
    expect(result.current.result).toBeDefined();

    act(() => result.current.reset());
    expect(result.current.result).toBeUndefined();
    expect(result.current.problem).toBeUndefined();
  });

  it('execute returns CommandOutcome with kind success', async () => {
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: { upload_intent_id: 'ui2' },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() =>
      useDomainCommand('create_upload_intent')
    );

    let outcome: any;
    await act(async () => {
      outcome = await result.current.execute({
        body: {} as any,
        actionInstanceId: 'outcome-test-1',
      });
    });

    expect(outcome.kind).toBe('success');
    expect(outcome.data).toEqual({ upload_intent_id: 'ui2' });
  });

  it('execute returns CommandOutcome with kind problem', async () => {
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'problem',
      status: 422,
      problem: {
        type: 'validation',
        title: 'Bad',
        status: 422,
        code: 'validation_error',
      },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() =>
      useDomainCommand('create_upload_intent')
    );

    let outcome: any;
    await act(async () => {
      outcome = await result.current.execute({
        body: {} as any,
        actionInstanceId: 'outcome-test-2',
      });
    });

    expect(outcome.kind).toBe('problem');
    expect(outcome.problem.code).toBe('validation_error');
  });

  it('execute returns CommandOutcome with kind reconciliation', async () => {
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'reconciliation_required',
      actionInstanceId: 'outcome-test-3',
      staleSince: 9999,
    });

    const { result } = renderHook(() =>
      useDomainCommand('create_upload_intent')
    );

    let outcome: any;
    await act(async () => {
      outcome = await result.current.execute({
        body: {} as any,
        actionInstanceId: 'outcome-test-3',
      });
    });

    expect(outcome.kind).toBe('reconciliation');
    expect(outcome.staleSince).toBe(9999);
  });

  it('exposes isPending during execute', async () => {
    let resolver: (v: any) => void;
    mockGateway.createUploadIntent.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolver = resolve;
        })
    );

    const { result } = renderHook(() =>
      useDomainCommand('create_upload_intent')
    );

    expect(result.current.isPending).toBe(false);

    let promise: Promise<any>;
    act(() => {
      promise = result.current.execute({
        body: {} as any,
        actionInstanceId: 'pending-test',
      });
    });

    expect(result.current.isPending).toBe(true);

    await act(async () => {
      resolver!({
        kind: 'success',
        status: 201,
        data: {},
        meta: { etag: null, retryAt: null, location: null },
      });
      await promise!;
    });

    expect(result.current.isPending).toBe(false);
  });

  it('retry without prior execute returns no_previous_command problem', async () => {
    const { result } = renderHook(() =>
      useDomainCommand('create_upload_intent')
    );

    let outcome: any;
    await act(async () => {
      outcome = await result.current.retry();
    });

    expect(outcome.kind).toBe('problem');
    expect(outcome.problem.code).toBe('no_previous_command');
  });

  it.each([
    [
      'submit_localization_claim_decisions' as const,
      mockGateway.submitLocalizationClaimDecisions,
      { decisions: [] },
    ],
    [
      'approve_localization_copy' as const,
      mockGateway.approveLocalizationCopy,
      { copy_version_id: 'lcv_1' },
    ],
  ])('forwards If-Match for %s', async (operationId, gatewayMethod, body) => {
    gatewayMethod.mockResolvedValue({
      kind: 'success',
      status: 200,
      data: {},
      meta: { etag: null, retryAt: null, location: null },
    });
    const { result } = renderHook(() => useDomainCommand(operationId));

    await act(async () => {
      await result.current.execute({
        pathParams: { batch_id: 'lb_1' },
        body: body as any,
        actionInstanceId: `cas-${operationId}`,
        ifMatch: 7,
      });
    });

    expect(gatewayMethod).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        actionInstanceId: `cas-${operationId}`,
        batchId: 'lb_1',
        ifMatch: 7,
      })
    );
  });
});
