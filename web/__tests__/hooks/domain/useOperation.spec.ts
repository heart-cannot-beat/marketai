/**
 * @jest-environment jsdom
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { useOperation } from '@/hooks/domain/useOperation';

const mockGateway = {
  pollOperation: jest.fn(),
};

// A mutable module-level value (not a fixed object literal) so the
// "workspace switch mid-poll" regression test below can flip `.slug` and
// force `useWorkspace()` to return a new value on the hook's next render,
// without unmounting the component under test — the scenario a Pages
// Router workspace switch produces. Must be prefixed `mock*` for
// babel-plugin-jest-hoist to allow this out-of-scope reference inside the
// `jest.mock` factory.
let mockCurrentWorkspace = {
  slug: 'acme',
  workspaceId: 'w1',
  role: 'OWNER',
  membershipId: 'm1',
  displayName: 'Acme',
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: mockCurrentWorkspace }),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));

beforeEach(() => {
  jest.useFakeTimers();
  jest.clearAllMocks();
  sessionStorage.clear();
  mockCurrentWorkspace = {
    slug: 'acme',
    workspaceId: 'w1',
    role: 'OWNER',
    membershipId: 'm1',
    displayName: 'Acme',
  };
});

afterEach(() => jest.useRealTimers());

describe('useOperation', () => {
  it('does not poll when operationRecordId is null', () => {
    renderHook(() =>
      useOperation({
        operationRecordId: null,
        purpose: 'upload',
        actionInstanceId: 'a1',
      })
    );
    expect(mockGateway.pollOperation).not.toHaveBeenCalled();
  });

  it('polls and returns running state', async () => {
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: {
        operation_id: 'op1',
        command_type: 'upload',
        state: 'running',
        result: null,
        created_at: '2026-08-31T00:00:00Z',
        updated_at: '2026-08-31T00:00:00Z',
      },
      meta: { etag: '"v1"', retryAt: null, location: null },
    });

    const { result } = renderHook(() =>
      useOperation({
        operationRecordId: 'op1',
        purpose: 'upload',
        actionInstanceId: 'a1',
      })
    );

    await waitFor(() => expect(result.current.operation).toBeDefined());
    expect(result.current.isTerminal).toBe(false);
    expect(result.current.operation?.state).toBe('running');
  });

  it('stops polling on succeeded terminal state', async () => {
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: {
        operation_id: 'op2',
        command_type: 'upload',
        state: 'succeeded',
        result: { resource_type: 'asset', resource_id: 'r1' },
        created_at: '2026-08-31T00:00:00Z',
        updated_at: '2026-08-31T00:00:00Z',
      },
      meta: { etag: '"v2"', retryAt: null, location: null },
    });

    const { result } = renderHook(() =>
      useOperation({
        operationRecordId: 'op2',
        purpose: 'upload',
        actionInstanceId: 'a2',
      })
    );

    await waitFor(() => expect(result.current.isTerminal).toBe(true));
    const callCount = mockGateway.pollOperation.mock.calls.length;
    jest.advanceTimersByTime(30_000);
    expect(mockGateway.pollOperation).toHaveBeenCalledTimes(callCount);
  });

  it('treats partially_succeeded as terminal', async () => {
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: {
        operation_id: 'op3',
        command_type: 'gen',
        state: 'partially_succeeded',
        result: { resource_type: 'asset', resource_id: 'r2' },
        created_at: '2026-08-31T00:00:00Z',
        updated_at: '2026-08-31T00:00:00Z',
      },
      meta: { etag: '"v3"', retryAt: null, location: null },
    });

    const { result } = renderHook(() =>
      useOperation({
        operationRecordId: 'op3',
        purpose: 'gen',
        actionInstanceId: 'a3',
      })
    );

    await waitFor(() => expect(result.current.isTerminal).toBe(true));
    expect(result.current.isPartialSuccess).toBe(true);
  });

  it('continues polling on uncertain state', async () => {
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: {
        operation_id: 'op4',
        command_type: 'gen',
        state: 'uncertain',
        result: null,
        created_at: '2026-08-31T00:00:00Z',
        updated_at: '2026-08-31T00:00:00Z',
      },
      meta: { etag: '"v4"', retryAt: null, location: null },
    });

    const { result } = renderHook(() =>
      useOperation({
        operationRecordId: 'op4',
        purpose: 'gen',
        actionInstanceId: 'a4',
      })
    );

    await waitFor(() => expect(result.current.operation).toBeDefined());
    expect(result.current.isTerminal).toBe(false);
  });

  it('persists operation to sessionStorage for refresh recovery', async () => {
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: {
        operation_id: 'op5',
        command_type: 'upload',
        state: 'running',
        result: null,
        created_at: '2026-08-31T00:00:00Z',
        updated_at: '2026-08-31T00:00:00Z',
      },
      meta: { etag: '"v5"', retryAt: null, location: null },
    });

    renderHook(() =>
      useOperation({
        operationRecordId: 'op5',
        purpose: 'upload',
        actionInstanceId: 'recovery-test',
      })
    );

    await waitFor(() =>
      expect(sessionStorage.getItem('operation:acme:recovery-test')).toBe('op5')
    );
  });

  it('respects Retry-After from server', async () => {
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: {
        operation_id: 'op6',
        command_type: 'gen',
        state: 'running',
        result: null,
        created_at: '2026-08-31T00:00:00Z',
        updated_at: '2026-08-31T00:00:00Z',
      },
      meta: { etag: '"v6"', retryAt: 10, location: null },
    });

    renderHook(() =>
      useOperation({
        operationRecordId: 'op6',
        purpose: 'gen',
        actionInstanceId: 'a6',
      })
    );

    await waitFor(() =>
      expect(mockGateway.pollOperation).toHaveBeenCalledTimes(1)
    );
    act(() => {
      jest.advanceTimersByTime(5_000);
    });
    expect(mockGateway.pollOperation).toHaveBeenCalledTimes(1);
    await act(async () => {
      jest.advanceTimersByTime(6_000);
    });
    expect(mockGateway.pollOperation).toHaveBeenCalledTimes(2);
  });

  it('sends ETag as If-None-Match on subsequent polls', async () => {
    mockGateway.pollOperation
      .mockResolvedValueOnce({
        kind: 'ok',
        status: 200,
        data: {
          operation_id: 'op7',
          command_type: 'gen',
          state: 'running',
          result: null,
          created_at: '2026-08-31T00:00:00Z',
          updated_at: '2026-08-31T00:00:00Z',
        },
        meta: { etag: '"first"', retryAt: 1, location: null },
      })
      .mockResolvedValueOnce({
        kind: 'not_modified',
        status: 304,
        data: null,
        meta: { etag: '"first"', retryAt: 1, location: null },
      });

    renderHook(() =>
      useOperation({
        operationRecordId: 'op7',
        purpose: 'gen',
        actionInstanceId: 'a7',
      })
    );

    await waitFor(() =>
      expect(mockGateway.pollOperation).toHaveBeenCalledTimes(1)
    );
    jest.advanceTimersByTime(2_000);
    await waitFor(() =>
      expect(mockGateway.pollOperation).toHaveBeenCalledTimes(2)
    );

    expect(mockGateway.pollOperation.mock.calls[1][0].ifNoneMatch).toBe(
      '"first"'
    );
  });

  it('keeps previous data across a 304 not_modified response', async () => {
    mockGateway.pollOperation
      .mockResolvedValueOnce({
        kind: 'ok',
        status: 200,
        data: {
          operation_id: 'op8',
          command_type: 'gen',
          state: 'running',
          result: null,
          created_at: '2026-08-31T00:00:00Z',
          updated_at: '2026-08-31T00:00:00Z',
        },
        meta: { etag: '"first"', retryAt: 1, location: null },
      })
      .mockResolvedValueOnce({
        kind: 'not_modified',
        status: 304,
        data: null,
        meta: { etag: '"first"', retryAt: 1, location: null },
      });

    const { result } = renderHook(() =>
      useOperation({
        operationRecordId: 'op8',
        purpose: 'gen',
        actionInstanceId: 'a8',
      })
    );

    await waitFor(() =>
      expect(result.current.operation?.state).toBe('running')
    );
    jest.advanceTimersByTime(2_000);
    await waitFor(() =>
      expect(mockGateway.pollOperation).toHaveBeenCalledTimes(2)
    );

    expect(result.current.operation?.operation_id).toBe('op8');
    expect(result.current.operation?.state).toBe('running');
  });

  it('surfaces a problem response and stops polling', async () => {
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'problem',
      status: 404,
      problem: {
        type: 'about:blank',
        title: 'Not Found',
        status: 404,
        detail: '',
        instance: '',
        code: 'operation_not_found',
        trace_id: 't1',
        retryable: false,
        field_errors: [],
      },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() =>
      useOperation({
        operationRecordId: 'op9',
        purpose: 'gen',
        actionInstanceId: 'a9',
      })
    );

    await waitFor(() => expect(result.current.problem).toBeDefined());
    expect(result.current.problem?.code).toBe('operation_not_found');
    expect(result.current.operation).toBeUndefined();

    const callCount = mockGateway.pollOperation.mock.calls.length;
    jest.advanceTimersByTime(30_000);
    expect(mockGateway.pollOperation).toHaveBeenCalledTimes(callCount);
  });

  it('re-targets the poll loop to the new workspace slug on a workspace switch', async () => {
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: {
        operation_id: 'op10',
        command_type: 'gen',
        state: 'running',
        result: null,
        created_at: '2026-08-31T00:00:00Z',
        updated_at: '2026-08-31T00:00:00Z',
      },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { rerender } = renderHook(() =>
      useOperation({
        operationRecordId: 'op10',
        purpose: 'gen',
        actionInstanceId: 'a10',
      })
    );

    await waitFor(() =>
      expect(mockGateway.pollOperation).toHaveBeenCalledTimes(1)
    );
    expect(mockGateway.pollOperation.mock.calls[0][0].workspaceSlug).toBe(
      'acme'
    );

    // Simulate a Workspace switch that doesn't unmount this component (the
    // Pages Router shell keeps it mounted across a `switchWorkspace`) —
    // `useWorkspace()` now returns a different slug on the next render.
    mockCurrentWorkspace = { ...mockCurrentWorkspace, slug: 'beta' };
    await act(async () => {
      rerender();
    });

    await waitFor(() =>
      expect(mockGateway.pollOperation).toHaveBeenCalledTimes(2)
    );
    expect(mockGateway.pollOperation.mock.calls[1][0].workspaceSlug).toBe(
      'beta'
    );

    // The stale `acme`-scoped closure must not still be driving the timer
    // loop underneath the fresh one — every poll from here on targets the
    // new Workspace.
    await act(async () => {
      jest.advanceTimersByTime(30_000);
    });
    await waitFor(() =>
      expect(mockGateway.pollOperation.mock.calls.length).toBeGreaterThan(2)
    );
    const slugsPolled = mockGateway.pollOperation.mock.calls
      .slice(1)
      .map((call) => call[0].workspaceSlug);
    expect(slugsPolled.every((slug) => slug === 'beta')).toBe(true);
  });
});
